import inspect
import types
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    ClassVar,
    Deque,
    Dict,
    Generator,
    Generic,
    List,
    Literal,
    Mapping,
    NamedTuple,
    Optional,
    Protocol,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from django.core.exceptions import ImproperlyConfigured
from django.forms.widgets import Media as MediaCls
from django.http import HttpRequest, HttpResponse
from django.template.base import NodeList, Parser, Template, TextNode, Token
from django.template.context import Context, RequestContext
from django.template.loader import get_template
from django.template.loader_tags import BLOCK_CONTEXT_KEY
from django.test.signals import template_rendered
from django.utils.html import conditional_escape
from django.views import View

from django_components.app_settings import ContextBehavior
from django_components.component_media import ComponentMediaInput, ComponentMediaMeta
from django_components.component_registry import ComponentRegistry
from django_components.component_registry import registry as registry_
from django_components.context import (
    _COMPONENT_SLOT_CTX_CONTEXT_KEY,
    _REGISTRY_CONTEXT_KEY,
    _ROOT_CTX_CONTEXT_KEY,
    get_injected_context_var,
    make_isolated_context_copy,
)
from django_components.dependencies import (
    RenderType,
    cache_component_css,
    cache_component_css_vars,
    cache_component_js,
    cache_component_js_vars,
    postprocess_component_html,
)
from django_components.node import BaseNode
from django_components.slots import (
    ComponentSlotContext,
    Slot,
    SlotContent,
    SlotFunc,
    SlotIsFilled,
    SlotName,
    SlotRef,
    SlotResult,
    _is_extracting_fill,
    _nodelist_to_slot_render_func,
    resolve_fills,
)
from django_components.template import cached_template
from django_components.util.django_monkeypatch import is_template_cls_patched
from django_components.util.misc import gen_id
from django_components.util.template_tag import TagAttr
from django_components.util.validation import validate_typed_dict, validate_typed_tuple

# TODO_REMOVE_IN_V1 - Users should use top-level import instead
# isort: off
from django_components.component_registry import AlreadyRegistered as AlreadyRegistered  # NOQA
from django_components.component_registry import ComponentRegistry as ComponentRegistry  # NOQA
from django_components.component_registry import NotRegistered as NotRegistered  # NOQA
from django_components.component_registry import register as register  # NOQA
from django_components.component_registry import registry as registry  # NOQA

# isort: on

COMP_ONLY_FLAG = "only"

# Define TypeVars for args and kwargs
ArgsType = TypeVar("ArgsType", bound=tuple, contravariant=True)
KwargsType = TypeVar("KwargsType", bound=Mapping[str, Any], contravariant=True)
SlotsType = TypeVar("SlotsType", bound=Mapping[SlotName, SlotContent])
DataType = TypeVar("DataType", bound=Mapping[str, Any], covariant=True)
JsDataType = TypeVar("JsDataType", bound=Mapping[str, Any])
CssDataType = TypeVar("CssDataType", bound=Mapping[str, Any])


@dataclass(frozen=True)
class RenderInput(Generic[ArgsType, KwargsType, SlotsType]):
    id: str
    context: Context
    args: ArgsType
    kwargs: KwargsType
    slots: SlotsType
    type: RenderType
    render_dependencies: bool


@dataclass()
class RenderStackItem(Generic[ArgsType, KwargsType, SlotsType]):
    input: RenderInput[ArgsType, KwargsType, SlotsType]
    is_filled: Optional[SlotIsFilled]


class ViewFn(Protocol):
    def __call__(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any: ...  # noqa: E704


class ComponentVars(NamedTuple):
    """
    Type for the variables available inside the component templates.

    All variables here are scoped under `component_vars.`, so e.g. attribute
    `is_filled` on this class is accessible inside the template as:

    ```django
    {{ component_vars.is_filled }}
    ```
    """

    is_filled: Dict[str, bool]
    """
    Dictonary describing which component slots are filled (`True`) or are not (`False`).

    <i>New in version 0.70</i>

    Use as `{{ component_vars.is_filled }}`

    Example:

    ```django
    {# Render wrapping HTML only if the slot is defined #}
    {% if component_vars.is_filled.my_slot %}
        <div class="slot-wrapper">
            {% slot "my_slot" / %}
        </div>
    {% endif %}
    ```

    This is equivalent to checking if a given key is among the slot fills:

    ```py
    class MyTable(Component):
        def get_context_data(self, *args, **kwargs):
            return {
                "my_slot_filled": "my_slot" in self.input.slots
            }
    ```
    """


# Descriptor to pass getting/setting of `template_name` onto `template_file`
class ComponentTemplateNameDescriptor:
    def __get__(self, instance: Optional["Component"], cls: Type["Component"]) -> Any:
        obj = instance if instance is not None else cls
        return obj.template_file  # type: ignore[attr-defined]

    def __set__(self, instance_or_cls: Union["Component", Type["Component"]], value: Any) -> None:
        cls = instance_or_cls if isinstance(instance_or_cls, type) else instance_or_cls.__class__
        cls.template_file = value


class ComponentMeta(ComponentMediaMeta):
    def __new__(mcs, name: Any, bases: Tuple, attrs: Dict) -> Any:
        # If user set `template_name` on the class, we instead set it to `template_file`,
        # because we want `template_name` to be the descriptor that proxies to `template_file`.
        if "template_name" in attrs:
            attrs["template_file"] = attrs.pop("template_name")
        attrs["template_name"] = ComponentTemplateNameDescriptor()

        return super().__new__(mcs, name, bases, attrs)


# NOTE: We use metaclass to automatically define the HTTP methods as defined
# in `View.http_method_names`.
class ComponentViewMeta(type):
    def __new__(mcs, name: str, bases: Any, dct: Dict) -> Any:
        # Default implementation shared by all HTTP methods
        def create_handler(method: str) -> Callable:
            def handler(self, request: HttpRequest, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
                component: "Component" = self.component
                return getattr(component, method)(request, *args, **kwargs)

            return handler

        # Add methods to the class
        for method_name in View.http_method_names:
            if method_name not in dct:
                dct[method_name] = create_handler(method_name)

        return super().__new__(mcs, name, bases, dct)


class ComponentView(View, metaclass=ComponentViewMeta):
    """
    Subclass of `django.views.View` where the `Component` instance is available
    via `self.component`.
    """

    # NOTE: This attribute must be declared on the class for `View.as_view` to allow
    # us to pass `component` kwarg.
    component = cast("Component", None)

    def __init__(self, component: "Component", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.component = component


class Component(
    Generic[ArgsType, KwargsType, SlotsType, DataType, JsDataType, CssDataType],
    metaclass=ComponentMeta,
):
    # #####################################
    # PUBLIC API (Configurable by users)
    # #####################################

    template_file: Optional[str] = None
    """
    Filepath to the Django template associated with this component.

    The filepath must be either:

    - Relative to the directory where the Component's Python file is defined.
    - Relative to one of the component directories, as set by
      [`COMPONENTS.dirs`](../settings.md#django_components.app_settings.ComponentsSettings.dirs)
      or
      [`COMPONENTS.app_dirs`](../settings.md#django_components.app_settings.ComponentsSettings.app_dirs)
      (e.g. `<root>/components/`).
    - Relative to the template directories, as set by Django's `TEMPLATES` setting (e.g. `<root>/templates/`).

    Only one of [`template_file`](../api#django_components.Component.template_file),
    [`get_template_name`](../api#django_components.Component.get_template_name),
    [`template`](../api#django_components.Component.template)
    or [`get_template`](../api#django_components.Component.get_template) must be defined.

    **Example:**

    ```py
    class MyComponent(Component):
        template_file = "path/to/template.html"

        def get_context_data(self):
            return {"name": "World"}
    ```
    """

    # NOTE: This attribute is managed by `ComponentTemplateNameDescriptor` that's set in the metaclass.
    #       But we still define it here for documenting and type hinting.
    template_name: Optional[str]
    """
    Alias for [`template_file`](../api#django_components.Component.template_file).

    For historical reasons, django-components used `template_name` to align with Django's
    [TemplateView](https://docs.djangoproject.com/en/5.1/ref/class-based-views/base/#django.views.generic.base.TemplateView).

    `template_file` was introduced to align with `js/js_file` and `css/css_file`.

    Setting and accessing this attribute is proxied to `template_file`.
    """

    def get_template_name(self, context: Context) -> Optional[str]:
        """
        Filepath to the Django template associated with this component.

        The filepath must be relative to either the file where the component class was defined,
        or one of the roots of `STATIFILES_DIRS`.

        Only one of [`template_file`](../api#django_components.Component.template_file),
        [`get_template_name`](../api#django_components.Component.get_template_name),
        [`template`](../api#django_components.Component.template)
        or [`get_template`](../api#django_components.Component.get_template) must be defined.
        """
        return None

    template: Optional[Union[str, Template]] = None
    """
    Inlined Django template associated with this component. Can be a plain string or a Template instance.

    Only one of [`template_file`](../api#django_components.Component.template_file),
    [`get_template_name`](../api#django_components.Component.get_template_name),
    [`template`](../api#django_components.Component.template)
    or [`get_template`](../api#django_components.Component.get_template) must be defined.

    **Example:**

    ```py
    class MyComponent(Component):
        template = "Hello, {{ name }}!"

        def get_context_data(self):
            return {"name": "World"}
    ```
    """

    def get_template(self, context: Context) -> Optional[Union[str, Template]]:
        """
        Inlined Django template associated with this component. Can be a plain string or a Template instance.

        Only one of [`template_file`](../api#django_components.Component.template_file),
        [`get_template_name`](../api#django_components.Component.get_template_name),
        [`template`](../api#django_components.Component.template)
        or [`get_template`](../api#django_components.Component.get_template) must be defined.
        """
        return None

    def get_context_data(self, *args: Any, **kwargs: Any) -> DataType:
        return cast(DataType, {})

    js: Optional[str] = None
    """
    Main JS associated with this component inlined as string.

    Only one of [`js`](../api#django_components.Component.js) or
    [`js_file`](../api#django_components.Component.js_file) must be defined.

    **Example:**

    ```py
    class MyComponent(Component):
        js = "console.log('Hello, World!');"
    ```
    """

    js_file: Optional[str] = None
    """
    Main JS associated with this component as file path.

    The filepath must be either:

    - Relative to the directory where the Component's Python file is defined.
    - Relative to one of the component directories, as set by
      [`COMPONENTS.dirs`](../settings.md#django_components.app_settings.ComponentsSettings.dirs)
      or
      [`COMPONENTS.app_dirs`](../settings.md#django_components.app_settings.ComponentsSettings.app_dirs)
      (e.g. `<root>/components/`).
    - Relative to the staticfiles directories, as set by Django's `STATICFILES_DIRS` setting (e.g. `<root>/static/`).

    When you create a Component class with `js_file`, these will happen:

    1. If the file path is relative to the directory where the component's Python file is,
       the path is resolved.
    2. The file is read and its contents is set to [`Component.js`](../api#django_components.Component.js).

    Only one of [`js`](../api#django_components.Component.js) or
    [`js_file`](../api#django_components.Component.js_file) must be defined.

    **Example:**

    ```js title="path/to/script.js"
    console.log('Hello, World!');
    ```

    ```py title="path/to/component.py"
    class MyComponent(Component):
        js_file = "path/to/script.js"

    print(MyComponent.js)
    # Output: console.log('Hello, World!');
    ```
    """

    css: Optional[str] = None
    """
    Main CSS associated with this component inlined as string.

    Only one of [`css`](../api#django_components.Component.css) or
    [`css_file`](../api#django_components.Component.css_file) must be defined.

    **Example:**

    ```py
    class MyComponent(Component):
        css = \"\"\"
        .my-class {
            color: red;
        }
        \"\"\"
    ```
    """

    css_file: Optional[str] = None
    """
    Main CSS associated with this component as file path.

    The filepath must be either:

    - Relative to the directory where the Component's Python file is defined.
    - Relative to one of the component directories, as set by
      [`COMPONENTS.dirs`](../settings.md#django_components.app_settings.ComponentsSettings.dirs)
      or
      [`COMPONENTS.app_dirs`](../settings.md#django_components.app_settings.ComponentsSettings.app_dirs)
      (e.g. `<root>/components/`).
    - Relative to the staticfiles directories, as set by Django's `STATICFILES_DIRS` setting (e.g. `<root>/static/`).

    When you create a Component class with `css_file`, these will happen:

    1. If the file path is relative to the directory where the component's Python file is,
       the path is resolved.
    2. The file is read and its contents is set to [`Component.css`](../api#django_components.Component.css).

    Only one of [`css`](../api#django_components.Component.css) or
    [`css_file`](../api#django_components.Component.css_file) must be defined.

    **Example:**

    ```css title="path/to/style.css"
    .my-class {
        color: red;
    }
    ```

    ```py title="path/to/component.py"
    class MyComponent(Component):
        css_file = "path/to/style.css"

    print(MyComponent.css)
    # Output:
    # .my-class {
    #     color: red;
    # };
    ```
    """

    media: Optional[MediaCls] = None
    """
    Normalized definition of JS and CSS media files associated with this component.
    `None` if [`Component.Media`](../api#django_components.Component.Media) is not defined.

    This field is generated from [`Component.media_class`](../api#django_components.Component.media_class).

    Read more on [Accessing component's HTML / JS / CSS](../../concepts/fundamentals/defining_js_css_html_files/#accessing-components-media-files).

    **Example:**

    ```py
    class MyComponent(Component):
        class Media:
            js = "path/to/script.js"
            css = "path/to/style.css"

    print(MyComponent.media)
    # Output:
    # <script src="/static/path/to/script.js"></script>
    # <link href="/static/path/to/style.css" media="all" rel="stylesheet">
    ```
    """  # noqa: E501

    media_class: Type[MediaCls] = MediaCls
    """
    Set the [Media class](https://docs.djangoproject.com/en/5.1/topics/forms/media/#assets-as-a-static-definition)
    that will be instantiated with the JS and CSS media files from
    [`Component.Media`](../api#django_components.Component.Media).

    This is useful when you want to customize the behavior of the media files, like
    customizing how the JS or CSS files are rendered into `<script>` or `<link>` HTML tags.
    Read more in [Defining HTML / JS / CSS files](../../concepts/fundamentals/defining_js_css_html_files/#customize-how-paths-are-rendered-into-html-tags-with-media_class).

    **Example:**

    ```py
    class MyTable(Component):
        class Media:
            js = "path/to/script.js"
            css = "path/to/style.css"

        media_class = MyMediaClass
    ```
    """  # noqa: E501

    Media: Optional[Type[ComponentMediaInput]] = None
    """
    Defines JS and CSS media files associated with this component.

    This `Media` class behaves similarly to
    [Django's Media class](https://docs.djangoproject.com/en/5.1/topics/forms/media/#assets-as-a-static-definition):

    - Paths are generally handled as static file paths, and resolved URLs are rendered to HTML with
      `media_class.render_js()` or `media_class.render_css()`.
    - A path that starts with `http`, `https`, or `/` is considered a URL, skipping the static file resolution.
      This path is still rendered to HTML with `media_class.render_js()` or `media_class.render_css()`.
    - A `SafeString` (with `__html__` method) is considered an already-formatted HTML tag, skipping both static file
        resolution and rendering with `media_class.render_js()` or `media_class.render_css()`.
    - You can set [`extend`](../api#django_components.ComponentMediaInput.extend) to configure
      whether to inherit JS / CSS from parent components. See
      [Controlling Media Inheritance](../../concepts/fundamentals/defining_js_css_html_files/#controlling-media-inheritance).

    However, there's a few differences from Django's Media class:

    1. Our Media class accepts various formats for the JS and CSS files: either a single file, a list,
       or (CSS-only) a dictonary (See [`ComponentMediaInput`](../api#django_components.ComponentMediaInput)).
    2. Individual JS / CSS files can be any of `str`, `bytes`, `Path`,
       [`SafeString`](https://dev.to/doridoro/django-safestring-afj), or a function
       (See [`ComponentMediaInputPath`](../api#django_components.ComponentMediaInputPath)).

    **Example:**

    ```py
    class MyTable(Component):
        class Media:
            js = [
                "path/to/script.js",
                "https://unpkg.com/alpinejs@3.14.7/dist/cdn.min.js",  # AlpineJS
            ]
            css = {
                "all": [
                    "path/to/style.css",
                    "https://unpkg.com/tailwindcss@^2/dist/tailwind.min.css",  # TailwindCSS
                ],
                "print": ["path/to/style2.css"],
            }
    ```
    """  # noqa: E501

    response_class = HttpResponse
    """This allows to configure what class is used to generate response from `render_to_response`"""
    View = ComponentView

    # #####################################
    # PUBLIC API - HOOKS
    # #####################################

    def on_render_before(self, context: Context, template: Template) -> None:
        """
        Hook that runs just before the component's template is rendered.

        You can use this hook to access or modify the context or the template.
        """
        pass

    def on_render_after(self, context: Context, template: Template, content: str) -> Optional[SlotResult]:
        """
        Hook that runs just after the component's template was rendered.
        It receives the rendered output as the last argument.

        You can use this hook to access the context or the template, but modifying
        them won't have any effect.

        To override the content that gets rendered, you can return a string or SafeString
        from this hook.
        """
        pass

    # #####################################
    # MISC
    # #####################################

    _class_hash: ClassVar[int]

    def __init__(
        self,
        registered_name: Optional[str] = None,
        outer_context: Optional[Context] = None,
        registry: Optional[ComponentRegistry] = None,  # noqa F811
    ):
        # When user first instantiates the component class before calling
        # `render` or `render_to_response`, then we want to allow the render
        # function to make use of the instantiated object.
        #
        # So while `MyComp.render()` creates a new instance of MyComp internally,
        # if we do `MyComp(registered_name="abc").render()`, then we use the
        # already-instantiated object.
        #
        # To achieve that, we want to re-assign the class methods as instance methods.
        # For that we have to "unwrap" the class methods via __func__.
        # See https://stackoverflow.com/a/76706399/9788634
        self.render_to_response = types.MethodType(self.__class__.render_to_response.__func__, self)  # type: ignore
        self.render = types.MethodType(self.__class__.render.__func__, self)  # type: ignore
        self.as_view = types.MethodType(self.__class__.as_view.__func__, self)  # type: ignore

        self.registered_name: Optional[str] = registered_name
        self.outer_context: Context = outer_context or Context()
        self.registry = registry or registry_
        self._render_stack: Deque[RenderStackItem[ArgsType, KwargsType, SlotsType]] = deque()
        # None == uninitialized, False == No types, Tuple == types
        self._types: Optional[Union[Tuple[Any, Any, Any, Any, Any, Any], Literal[False]]] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        cls._class_hash = hash(inspect.getfile(cls) + cls.__name__)

    @property
    def name(self) -> str:
        return self.registered_name or self.__class__.__name__

    @property
    def id(self) -> str:
        """
        This ID is unique for every time a [`Component.render()`](../api#django_components.Component.render)
        (or equivalent) is called (AKA "render ID").

        This is useful for logging or debugging.

        Raises `RuntimeError` if accessed outside of rendering execution.

        A single render ID has a chance of collision 1 in 3.3M. However, due to birthday paradox, the chance of
        collision increases when approaching ~1,000 render IDs.

        **Thus, there is a soft-cap of 1,000 components rendered on a single page.**

        If you need to more than that, please open an issue on GitHub.

        **Example:**

        ```py
        class MyComponent(Component):
            def get_context_data(self):
                print(f"Rendering '{self.id}'")
                return {}

        MyComponent.render()
        # Rendering 'ab3c4d'
        ```
        """
        return self.input.id

    @property
    def input(self) -> RenderInput[ArgsType, KwargsType, SlotsType]:
        """
        Input holds the data (like arg, kwargs, slots) that were passsed to
        the current execution of the `render` method.
        """
        if not len(self._render_stack):
            raise RuntimeError(f"{self.name}: Tried to access Component input while outside of rendering execution")

        # NOTE: Input is managed as a stack, so if `render` is called within another `render`,
        # the propertes below will return only the inner-most state.
        return self._render_stack[-1].input

    @property
    def is_filled(self) -> SlotIsFilled:
        """
        Dictionary describing which slots have or have not been filled.

        This attribute is available for use only within the template as `{{ component_vars.is_filled.slot_name }}`,
        and within `on_render_before` and `on_render_after` hooks.
        """
        if not len(self._render_stack):
            raise RuntimeError(
                f"{self.name}: Tried to access Component's `is_filled` attribute "
                "while outside of rendering execution"
            )

        ctx = self._render_stack[-1]
        if ctx.is_filled is None:
            raise RuntimeError(
                f"{self.name}: Tried to access Component's `is_filled` attribute " "before slots were resolved"
            )

        return ctx.is_filled

    # NOTE: We cache the Template instance. When the template is taken from a file
    #       via `get_template_name`, then we leverage Django's template caching with `get_template()`.
    #       Otherwise, we use our own `cached_template()` to cache the template.
    #
    #       This is important to keep in mind, because the implication is that we should
    #       treat Templates AND their nodelists as IMMUTABLE.
    def _get_template(self, context: Context) -> Template:
        template_name = self.get_template_name(context)
        # TODO_REMOVE_IN_V1 - Remove `self.get_template_string` in v1
        template_getter = getattr(self, "get_template_string", self.get_template)
        template_body = template_getter(context)

        # `get_template_name()`, `get_template()`, and `template` are mutually exclusive
        #
        # Note that `template` and `template_name` are also mutually exclusive, but this
        # is checked when lazy-loading the template from `template_name`. So if user specified
        # `template_name`, then `template` will be populated with the content of that file.
        if self.template is not None and template_name is not None:
            raise ImproperlyConfigured(
                "Received non-null value from both 'template/template_name' and 'get_template_name' in"
                f" Component {type(self).__name__}. Only one of the two must be set."
            )
        if self.template is not None and template_body is not None:
            raise ImproperlyConfigured(
                "Received non-null value from both 'template/template_name' and 'get_template' in"
                f" Component {type(self).__name__}. Only one of the two must be set."
            )
        if template_name is not None and template_body is not None:
            raise ImproperlyConfigured(
                "Received non-null value from both 'get_template_name' and 'get_template' in"
                f" Component {type(self).__name__}. Only one of the two must be set."
            )

        if template_name is not None:
            return get_template(template_name).template

        template_body = template_body if template_body is not None else self.template
        if template_body is not None:
            # We got template string, so we convert it to Template
            if isinstance(template_body, str):
                template: Template = cached_template(
                    template_string=template_body,
                    name=self.template_file,
                )
            else:
                template = template_body

            return template

        raise ImproperlyConfigured(
            f"Either 'template_file' or 'template' must be set for Component {type(self).__name__}."
        )

    def inject(self, key: str, default: Optional[Any] = None) -> Any:
        """
        Use this method to retrieve the data that was passed to a `{% provide %}` tag
        with the corresponding key.

        To retrieve the data, `inject()` must be called inside a component that's
        inside the `{% provide %}` tag.

        You may also pass a default that will be used if the `provide` tag with given
        key was NOT found.

        This method mut be used inside the `get_context_data()` method and raises
        an error if called elsewhere.

        Example:

        Given this template:
        ```django
        {% provide "provider" hello="world" %}
            {% component "my_comp" %}
            {% endcomponent %}
        {% endprovide %}
        ```

        And given this definition of "my_comp" component:
        ```py
        from django_components import Component, register

        @register("my_comp")
        class MyComp(Component):
            template = "hi {{ data.hello }}!"
            def get_context_data(self):
                data = self.inject("provider")
                return {"data": data}
        ```

        This renders into:
        ```
        hi world!
        ```

        As the `{{ data.hello }}` is taken from the "provider".
        """
        if self.input is None:
            raise RuntimeError(
                f"Method 'inject()' of component '{self.name}' was called outside of 'get_context_data()'"
            )

        return get_injected_context_var(self.name, self.input.context, key, default)

    @classmethod
    def as_view(cls, **initkwargs: Any) -> ViewFn:
        """
        Shortcut for calling `Component.View.as_view` and passing component instance to it.
        """
        # This method may be called as class method or as instance method.
        # If called as class method, create a new instance.
        if isinstance(cls, Component):
            comp: Component = cls
        else:
            comp = cls()

        # Allow the View class to access this component via `self.component`
        return comp.View.as_view(**initkwargs, component=comp)

    # #####################################
    # RENDERING
    # #####################################

    @classmethod
    def render_to_response(
        cls,
        context: Optional[Union[Dict[str, Any], Context]] = None,
        slots: Optional[SlotsType] = None,
        escape_slots_content: bool = True,
        args: Optional[ArgsType] = None,
        kwargs: Optional[KwargsType] = None,
        type: RenderType = "document",
        request: Optional[HttpRequest] = None,
        *response_args: Any,
        **response_kwargs: Any,
    ) -> HttpResponse:
        """
        Render the component and wrap the content in the response class.

        The response class is taken from `Component.response_class`. Defaults to `django.http.HttpResponse`.

        This is the interface for the `django.views.View` class which allows us to
        use components as Django views with `component.as_view()`.

        Inputs:
        - `args` - Positional args for the component. This is the same as calling the component
          as `{% component "my_comp" arg1 arg2 ... %}`
        - `kwargs` - Kwargs for the component. This is the same as calling the component
          as `{% component "my_comp" key1=val1 key2=val2 ... %}`
        - `slots` - Component slot fills. This is the same as pasing `{% fill %}` tags to the component.
            Accepts a dictionary of `{ slot_name: slot_content }` where `slot_content` can be a string
            or render function.
        - `escape_slots_content` - Whether the content from `slots` should be escaped.
        - `context` - A context (dictionary or Django's Context) within which the component
          is rendered. The keys on the context can be accessed from within the template.
            - NOTE: In "isolated" mode, context is NOT accessible, and data MUST be passed via
              component's args and kwargs.
        - `type` - Configure how to handle JS and CSS dependencies.
            - `"document"` (default) - JS dependencies are inserted into `{% component_js_dependencies %}`,
              or to the end of the `<body>` tag. CSS dependencies are inserted into
              `{% component_css_dependencies %}`, or the end of the `<head>` tag.
        - `request` - The request object. This is only required when needing to use RequestContext,
                      e.g. to enable template `context_processors`. Unused if context is already an instance
                      of `Context`

        Any additional args and kwargs are passed to the `response_class`.

        Example:
        ```py
        MyComponent.render_to_response(
            args=[1, "two", {}],
            kwargs={
                "key": 123,
            },
            slots={
                "header": 'STATIC TEXT HERE',
                "footer": lambda ctx, slot_kwargs, slot_ref: f'CTX: {ctx['hello']} SLOT_DATA: {slot_kwargs['abc']}',
            },
            escape_slots_content=False,
            # HttpResponse input
            status=201,
            headers={...},
        )
        # HttpResponse(content=..., status=201, headers=...)
        ```
        """
        content = cls.render(
            args=args,
            kwargs=kwargs,
            context=context,
            slots=slots,
            escape_slots_content=escape_slots_content,
            type=type,
            render_dependencies=True,
            request=request,
        )
        return cls.response_class(content, *response_args, **response_kwargs)

    @classmethod
    def render(
        cls,
        context: Optional[Union[Dict[str, Any], Context]] = None,
        args: Optional[ArgsType] = None,
        kwargs: Optional[KwargsType] = None,
        slots: Optional[SlotsType] = None,
        escape_slots_content: bool = True,
        type: RenderType = "document",
        render_dependencies: bool = True,
        request: Optional[HttpRequest] = None,
    ) -> str:
        """
        Render the component into a string.

        Inputs:
        - `args` - Positional args for the component. This is the same as calling the component
          as `{% component "my_comp" arg1 arg2 ... %}`
        - `kwargs` - Kwargs for the component. This is the same as calling the component
          as `{% component "my_comp" key1=val1 key2=val2 ... %}`
        - `slots` - Component slot fills. This is the same as pasing `{% fill %}` tags to the component.
            Accepts a dictionary of `{ slot_name: slot_content }` where `slot_content` can be a string
            or render function.
        - `escape_slots_content` - Whether the content from `slots` should be escaped.
        - `context` - A context (dictionary or Django's Context) within which the component
          is rendered. The keys on the context can be accessed from within the template.
            - NOTE: In "isolated" mode, context is NOT accessible, and data MUST be passed via
              component's args and kwargs.
        - `type` - Configure how to handle JS and CSS dependencies.
            - `"document"` (default) - JS dependencies are inserted into `{% component_js_dependencies %}`,
              or to the end of the `<body>` tag. CSS dependencies are inserted into
              `{% component_css_dependencies %}`, or the end of the `<head>` tag.
        - `render_dependencies` - Set this to `False` if you want to insert the resulting HTML into another component.
        - `request` - The request object. This is only required when needing to use RequestContext,
                      e.g. to enable template `context_processors`. Unused if context is already an instance of
                      `Context`
        Example:
        ```py
        MyComponent.render(
            args=[1, "two", {}],
            kwargs={
                "key": 123,
            },
            slots={
                "header": 'STATIC TEXT HERE',
                "footer": lambda ctx, slot_kwargs, slot_ref: f'CTX: {ctx['hello']} SLOT_DATA: {slot_kwargs['abc']}',
            },
            escape_slots_content=False,
        )
        ```
        """
        # This method may be called as class method or as instance method.
        # If called as class method, create a new instance.
        if isinstance(cls, Component):
            comp: Component = cls
        else:
            comp = cls()

        return comp._render(context, args, kwargs, slots, escape_slots_content, type, render_dependencies, request)

    # This is the internal entrypoint for the render function
    def _render(
        self,
        context: Optional[Union[Dict[str, Any], Context]] = None,
        args: Optional[ArgsType] = None,
        kwargs: Optional[KwargsType] = None,
        slots: Optional[SlotsType] = None,
        escape_slots_content: bool = True,
        type: RenderType = "document",
        render_dependencies: bool = True,
        request: Optional[HttpRequest] = None,
    ) -> str:
        try:
            return self._render_impl(
                context, args, kwargs, slots, escape_slots_content, type, render_dependencies, request
            )
        except Exception as err:
            # Nicely format the error message to include the component path.
            # E.g.
            # ```
            # KeyError: "An error occured while rendering components ProjectPage > ProjectLayoutTabbed >
            # Layout > RenderContextProvider > Base > TabItem:
            # Component 'TabItem' tried to inject a variable '_tab' before it was provided.
            # ```

            if not hasattr(err, "_components"):
                err._components = []  # type: ignore[attr-defined]

            components = getattr(err, "_components", [])

            # Access the exception's message, see https://stackoverflow.com/a/75549200/9788634
            if not components:
                orig_msg = err.args[0]
            else:
                orig_msg = err.args[0].split("\n", 1)[1]

            components.insert(0, self.name)
            comp_path = " > ".join(components)
            prefix = f"An error occured while rendering components {comp_path}:\n"

            err.args = (prefix + orig_msg,)  # tuple of one
            raise err

    def _render_impl(
        self,
        context: Optional[Union[Dict[str, Any], Context]] = None,
        args: Optional[ArgsType] = None,
        kwargs: Optional[KwargsType] = None,
        slots: Optional[SlotsType] = None,
        escape_slots_content: bool = True,
        type: RenderType = "document",
        render_dependencies: bool = True,
        request: Optional[HttpRequest] = None,
    ) -> str:
        # NOTE: We must run validation before we normalize the slots, because the normalization
        #       wraps them in functions.
        self._validate_inputs(args or (), kwargs or {}, slots or {})

        # Allow to provide no args/kwargs/slots/context
        args = cast(ArgsType, args or ())
        kwargs = cast(KwargsType, kwargs or {})
        slots_untyped = self._normalize_slot_fills(slots or {}, escape_slots_content)
        slots = cast(SlotsType, slots_untyped)
        context = context or (RequestContext(request) if request else Context())

        # Allow to provide a dict instead of Context
        # NOTE: This if/else is important to avoid nested Contexts,
        # See https://github.com/django-components/django-components/issues/414
        if not isinstance(context, Context):
            context = RequestContext(request, context) if request else Context(context)

        # Required for compatibility with Django's {% extends %} tag
        # See https://github.com/django-components/django-components/pull/859
        context.render_context.push({BLOCK_CONTEXT_KEY: context.render_context.get(BLOCK_CONTEXT_KEY, {})})

        # By adding the current input to the stack, we temporarily allow users
        # to access the provided context, slots, etc. Also required so users can
        # call `self.inject()` from within `get_context_data()`.
        self._render_stack.append(
            RenderStackItem(
                input=RenderInput(
                    id=gen_id(),
                    context=context,
                    slots=slots,
                    args=args,
                    kwargs=kwargs,
                    type=type,
                    render_dependencies=render_dependencies,
                ),
                is_filled=None,
            ),
        )

        context_data = self.get_context_data(*args, **kwargs)
        self._validate_outputs(data=context_data)

        # Process Component's JS and CSS
        js_data: Dict = {}  # TODO
        cache_component_js(self.__class__)
        js_input_hash = cache_component_js_vars(self.__class__, js_data) if js_data else None

        css_data: Dict = {}  # TODO
        cache_component_css(self.__class__)
        css_input_hash = cache_component_css_vars(self.__class__, css_data) if css_data else None

        with _prepare_template(self, context, context_data) as template:
            # For users, we expose boolean variables that they may check
            # to see if given slot was filled, e.g.:
            # `{% if variable > 8 and component_vars.is_filled.header %}`
            is_filled = SlotIsFilled(slots_untyped)
            self._render_stack[-1].is_filled = is_filled

            component_slot_ctx = ComponentSlotContext(
                component_name=self.name,
                template_name=template.name,
                fills=slots_untyped,
                is_dynamic_component=getattr(self, "_is_dynamic_component", False),
                # This field will be modified from within `SlotNodes.render()`:
                # - The `default_slot` will be set to the first slot that has the `default` attribute set.
                #   If multiple slots have the `default` attribute set, yet have different name, then
                #   we will raise an error.
                default_slot=None,
            )

            with context.update(
                {
                    # Private context fields
                    _ROOT_CTX_CONTEXT_KEY: self.outer_context,
                    _COMPONENT_SLOT_CTX_CONTEXT_KEY: component_slot_ctx,
                    _REGISTRY_CONTEXT_KEY: self.registry,
                    # NOTE: Public API for variables accessible from within a component's template
                    # See https://github.com/django-components/django-components/issues/280#issuecomment-2081180940
                    "component_vars": ComponentVars(
                        is_filled=is_filled,
                    ),
                }
            ):
                self.on_render_before(context, template)

                # Emit signal that the template is about to be rendered
                template_rendered.send(sender=self, template=self, context=context)
                # Get the component's HTML
                html_content = template.render(context)

                # Allow to optionally override/modify the rendered content
                new_output = self.on_render_after(context, template, html_content)
                html_content = new_output if new_output is not None else html_content

                output = postprocess_component_html(
                    component_cls=self.__class__,
                    component_id=self.id,
                    html_content=html_content,
                    css_input_hash=css_input_hash,
                    js_input_hash=js_input_hash,
                    type=type,
                    render_dependencies=render_dependencies,
                )

        # After rendering is done, remove the current state from the stack, which means
        # properties like `self.context` will no longer return the current state.
        self._render_stack.pop()
        context.render_context.pop()

        return output

    def _normalize_slot_fills(
        self,
        fills: Mapping[SlotName, SlotContent],
        escape_content: bool = True,
    ) -> Dict[SlotName, Slot]:
        # Preprocess slots to escape content if `escape_content=True`
        norm_fills = {}

        # NOTE: `gen_escaped_content_func` is defined as a separate function, instead of being inlined within
        #       the forloop, because the value the forloop variable points to changes with each loop iteration.
        def gen_escaped_content_func(content: SlotFunc) -> Slot:
            def content_fn(ctx: Context, slot_data: Dict, slot_ref: SlotRef) -> SlotResult:
                rendered = content(ctx, slot_data, slot_ref)
                return conditional_escape(rendered) if escape_content else rendered

            slot = Slot(content_func=cast(SlotFunc, content_fn))
            return slot

        for slot_name, content in fills.items():
            if content is None:
                continue
            elif not callable(content):
                slot = _nodelist_to_slot_render_func(
                    slot_name,
                    NodeList([TextNode(conditional_escape(content) if escape_content else content)]),
                    data_var=None,
                    default_var=None,
                )
            else:
                slot = gen_escaped_content_func(content)

            norm_fills[slot_name] = slot

        return norm_fills

    # #####################################
    # VALIDATION
    # #####################################

    def _get_types(self) -> Optional[Tuple[Any, Any, Any, Any, Any, Any]]:
        """
        Extract the types passed to the Component class.

        So if a component subclasses Component class like so

        ```py
        class MyComp(Component[MyArgs, MyKwargs, MySlots, MyData, MyJsData, MyCssData]):
            ...
        ```

        Then we want to extract the tuple (MyArgs, MyKwargs, MySlots, MyData, MyJsData, MyCssData).

        Returns `None` if types were not provided. That is, the class was subclassed
        as:

        ```py
        class MyComp(Component):
            ...
        ```
        """
        # For efficiency, the type extraction is done only once.
        # If `self._types` is `False`, that means that the types were not specified.
        # If `self._types` is `None`, then this is the first time running this method.
        # Otherwise, `self._types` should be a tuple of (Args, Kwargs, Data, Slots)
        if self._types == False:  # noqa: E712
            return None
        elif self._types:
            return self._types

        # Since a class can extend multiple classes, e.g.
        #
        # ```py
        # class MyClass(BaseOne, BaseTwo, ...):
        #     ...
        # ```
        #
        # Then we need to find the base class that is our `Component` class.
        #
        # NOTE: __orig_bases__ is a tuple of _GenericAlias
        # See https://github.com/python/cpython/blob/709ef004dffe9cee2a023a3c8032d4ce80513582/Lib/typing.py#L1244
        # And https://github.com/python/cpython/issues/101688
        generics_bases: Tuple[Any, ...] = self.__orig_bases__  # type: ignore[attr-defined]
        component_generics_base = None
        for base in generics_bases:
            origin_cls = base.__origin__
            if origin_cls == Component or issubclass(origin_cls, Component):
                component_generics_base = base
                break

        if not component_generics_base:
            # If we get here, it means that the Component class wasn't supplied any generics
            self._types = False
            return None

        # If we got here, then we've found ourselves the typed Component class, e.g.
        #
        # `Component(Tuple[int], MyKwargs, MySlots, Any, Any, Any)`
        #
        # By accessing the __args__, we access individual types between the brackets, so
        #
        # (Tuple[int], MyKwargs, MySlots, Any, Any, Any)
        args_type, kwargs_type, slots_type, data_type, js_data_type, css_data_type = component_generics_base.__args__

        self._types = args_type, kwargs_type, slots_type, data_type, js_data_type, css_data_type
        return self._types

    def _validate_inputs(self, args: Tuple, kwargs: Any, slots: Any) -> None:
        maybe_inputs = self._get_types()
        if maybe_inputs is None:
            return
        args_type, kwargs_type, slots_type, data_type, js_data_type, css_data_type = maybe_inputs

        # Validate args
        validate_typed_tuple(args, args_type, f"Component '{self.name}'", "positional argument")
        # Validate kwargs
        validate_typed_dict(kwargs, kwargs_type, f"Component '{self.name}'", "keyword argument")
        # Validate slots
        validate_typed_dict(slots, slots_type, f"Component '{self.name}'", "slot")

    def _validate_outputs(self, data: Any) -> None:
        maybe_inputs = self._get_types()
        if maybe_inputs is None:
            return
        args_type, kwargs_type, slots_type, data_type, js_data_type, css_data_type = maybe_inputs

        # Validate data
        validate_typed_dict(data, data_type, f"Component '{self.name}'", "data")


class ComponentNode(BaseNode):
    """
    Renders one of the components that was previously registered with
    [`@register()`](./api.md#django_components.register)
    decorator.

    **Args:**

    - `name` (str, required): Registered name of the component to render
    - All other args and kwargs are defined based on the component itself.

    If you defined a component `"my_table"`

    ```python
    from django_component import Component, register

    @register("my_table")
    class MyTable(Component):
        template = \"\"\"
          <table>
            <thead>
              {% for header in headers %}
                <th>{{ header }}</th>
              {% endfor %}
            </thead>
            <tbody>
              {% for row in rows %}
                <tr>
                  {% for cell in row %}
                    <td>{{ cell }}</td>
                  {% endfor %}
                </tr>
              {% endfor %}
            <tbody>
          </table>
        \"\"\"

        def get_context_data(self, rows: List, headers: List):
            return {
                "rows": rows,
                "headers": headers,
            }
    ```

    Then you can render this component by referring to `MyTable` via its
    registered name `"my_table"`:

    ```django
    {% component "my_table" rows=rows headers=headers ... / %}
    ```

    ### Component input

    Positional and keyword arguments can be literals or template variables.

    The component name must be a single- or double-quotes string and must
    be either:

    - The first positional argument after `component`:

        ```django
        {% component "my_table" rows=rows headers=headers ... / %}
        ```

    - Passed as kwarg `name`:

        ```django
        {% component rows=rows headers=headers name="my_table" ... / %}
        ```

    ### Inserting into slots

    If the component defined any [slots](../concepts/fundamentals/slots.md), you can
    pass in the content to be placed inside those slots by inserting [`{% fill %}`](#fill) tags,
    directly within the `{% component %}` tag:

    ```django
    {% component "my_table" rows=rows headers=headers ... / %}
      {% fill "pagination" %}
        < 1 | 2 | 3 >
      {% endfill %}
    {% endcomponent %}
    ```

    ### Isolating components

    By default, components behave similarly to Django's
    [`{% include %}`](https://docs.djangoproject.com/en/5.1/ref/templates/builtins/#include),
    and the template inside the component has access to the variables defined in the outer template.

    You can selectively isolate a component, using the `only` flag, so that the inner template
    can access only the data that was explicitly passed to it:

    ```django
    {% component "name" positional_arg keyword_arg=value ... only %}
    ```
    """

    tag = "component"
    end_tag = "endcomponent"
    allowed_flags = [COMP_ONLY_FLAG]

    def __init__(
        self,
        # ComponentNode inputs
        name: str,
        registry: ComponentRegistry,  # noqa F811
        # BaseNode inputs
        params: List[TagAttr],
        flags: Optional[Dict[str, bool]] = None,
        nodelist: Optional[NodeList] = None,
        node_id: Optional[str] = None,
    ) -> None:
        super().__init__(params=params, flags=flags, nodelist=nodelist, node_id=node_id)

        self.name = name
        self.registry = registry

    @classmethod
    def parse(  # type: ignore[override]
        cls,
        parser: Parser,
        token: Token,
        registry: ComponentRegistry,  # noqa F811
        name: str,
        start_tag: str,
        end_tag: str,
    ) -> "ComponentNode":
        # Set the component-specific start and end tags by subclassing the base node
        subcls_name = cls.__name__ + "_" + name
        subcls: Type[ComponentNode] = type(subcls_name, (cls,), {"tag": start_tag, "end_tag": end_tag})

        # Call `BaseNode.parse()` as if with the context of subcls.
        node: ComponentNode = super(cls, subcls).parse(  # type: ignore[attr-defined]
            parser,
            token,
            registry=registry,
            name=name,
        )
        return node

    def render(self, context: Context, *args: Any, **kwargs: Any) -> str:
        # Do not render nested `{% component %}` tags in other `{% component %}` tags
        # at the stage when we are determining if the latter has named fills or not.
        if _is_extracting_fill(context):
            return ""

        component_cls: Type[Component] = self.registry.get(self.name)

        slot_fills = resolve_fills(context, self.nodelist, self.name)

        component: Component = component_cls(
            registered_name=self.name,
            outer_context=context,
            registry=self.registry,
        )

        # Prevent outer context from leaking into the template of the component
        if self.flags[COMP_ONLY_FLAG] or self.registry.settings.context_behavior == ContextBehavior.ISOLATED:
            context = make_isolated_context_copy(context)

        output = component._render(
            context=context,
            args=args,
            kwargs=kwargs,
            slots=slot_fills,
            # NOTE: When we render components inside the template via template tags,
            # do NOT render deps, because this may be decided by outer component
            render_dependencies=False,
        )

        return output


@contextmanager
def _maybe_bind_template(context: Context, template: Template) -> Generator[None, Any, None]:
    if context.template is None:
        with context.bind_template(template):
            yield
    else:
        yield


@contextmanager
def _prepare_template(
    component: Component,
    context: Context,
    context_data: Any,
) -> Generator[Template, Any, None]:
    with context.update(context_data):
        # Associate the newly-created Context with a Template, otherwise we get
        # an error when we try to use `{% include %}` tag inside the template?
        # See https://github.com/django-components/django-components/issues/580
        # And https://github.com/django-components/django-components/issues/634
        template = component._get_template(context)

        if not is_template_cls_patched(template):
            raise RuntimeError(
                "Django-components received a Template instance which was not patched."
                "If you are using Django's Template class, check if you added django-components"
                "to INSTALLED_APPS. If you are using a custom template class, then you need to"
                "manually patch the class."
            )

        # Set `Template._djc_is_component_nested` based on whether we're currently INSIDE
        # the `{% extends %}` tag.
        # Part of fix for https://github.com/django-components/django-components/issues/508
        # See django_monkeypatch.py
        template._djc_is_component_nested = bool(context.render_context.get(BLOCK_CONTEXT_KEY))

        with _maybe_bind_template(context, template):
            yield template
