import json
import re

from django.contrib.contenttypes.models import ContentType
from django.template import (Library, Node, TemplateSyntaxError,
                             Variable, loader)
from django.urls import reverse
from django.utils.module_loading import import_string
from django.utils.safestring import mark_safe


from django_comments.templatetags.comments import (BaseCommentNode,
                                                   RenderCommentListNode)

from django_comments_xtd import (get_model as get_comment_model,
                                 get_reactions_enum)
from django_comments_xtd.api import frontend
from django_comments_xtd.conf import settings
from django_comments_xtd.models import max_thread_level_for_content_type
from django_comments_xtd.utils import get_app_model_options, get_html_id_suffix


register = Library()


class RenderXtdCommentListNode(RenderCommentListNode):
    """Render the comment list directly."""

    @classmethod
    def handle_token(cls, parser, token):
        """Class method to parse render_xtdcomment_list and return a Node."""
        tokens = token.split_contents()
        if tokens[1] != 'for':
            raise TemplateSyntaxError("Second argument in %r tag must be 'for'"
                                      % tokens[0])

        # {% render_xtdcomment_list for obj %}
        if len(tokens) == 3:
            return cls(object_expr=parser.compile_filter(tokens[2]))

        # {% render_cxtdomment_list for app.model pk %}
        elif len(tokens) == 4:
            return cls(
                ctype=BaseCommentNode.lookup_content_type(tokens[2], tokens[0]),
                object_pk_expr=parser.compile_filter(tokens[3])
            )

        # {% render_xtdcomment_list for [obj | app.model pk] [using tmpl] %}
        elif len(tokens) > 4:
            try:
                template_path = tokens[-1]
                num_tokens_between = tokens.index("using") - tokens.index("for")
                if  num_tokens_between == 2:
                    # {% render_xtdcomment_list for object using tmpl}
                    return cls(object_expr=parser.compile_filter(tokens[2]),
                               template_path=template_path)
                elif num_tokens_between == 3:
                    # {% render_xtdcomment_list for app.model pk using tmpl}
                    tag_t, app_t = tokens[0], tokens[2]
                    ctype = BaseCommentNode.lookup_content_type(app_t, tag_t)
                    return cls(ctype=ctype,
                               object_pk_expr=parser.compile_filter(tokens[3]),
                               template_path=template_path)
            except IndexError:
                msg = ("Wrong syntax in %r tag. Valid syntaxes are: "
                       "{% render_xtdcomment_list for [object] [using "
                       "<template>] %} or {% render_xtdcomment_list for "
                       "[app].[model] [obj_id] [using <tmpl>] %}")
                raise TemplateSyntaxError(msg % tokens[0])

    def __init__(self, *args, **kwargs):
        self.template_path = None
        if "template_path" in kwargs:
            self.template_path = kwargs.pop("template_path")
        super().__init__(*args, **kwargs)

    def render(self, context):
        ctype, object_pk = self.get_target_ctype_pk(context)
        if object_pk:
            template_search_list = [
                "comments/%s/%s/list.html" % (ctype.app_label, ctype.model),
                "comments/%s/list.html" % ctype.app_label,
                "comments/list.html"
            ]
            qs = self.get_queryset(context)
            comment_list = self.get_context_value_from_queryset(context, qs)
            context_dict = context.flatten()
            context_dict['comment_list'] = comment_list

            # Pass max_thread_level in the context.
            app_model = "%s.%s" % (ctype.app_label, ctype.model)
            MTL = settings.COMMENTS_XTD_MAX_THREAD_LEVEL_BY_APP_MODEL
            mtl = MTL.get(app_model, settings.COMMENTS_XTD_MAX_THREAD_LEVEL)
            context_dict.update({
                'max_thread_level': mtl,
                'reply_stack': [],  # List to control reply rendering.
                'show_nested': True
            })

            # get_app_model_options returns a dict like: {
            #     'who_can_post': 'all' | 'users',
            #     'check_input_allowed': 'string path to function',
            #     'comment_flagging_enabled': <boolean>,
            #     'comment_reactions_enabled': <boolean>,
            #     'object_reactions_enabled': <boolean>
            # }
            options = get_app_model_options(content_type=app_model)
            check_input_allowed_str = options.pop('check_input_allowed')
            check_func = import_string(check_input_allowed_str)
            target_obj = ctype.get_object_for_this_type(pk=object_pk)

            # Call the function that checks whether comments input
            # is still allowed on the given target_object. And add
            # the resulting boolean to the template context.
            #
            options['is_input_allowed'] = check_func(target_obj)
            context_dict.update(options)

            liststr = loader.render_to_string(
                self.template_path or template_search_list,
                context_dict
            )
            return liststr
        else:
            return ''


@register.tag
def render_xtdcomment_list(parser, token):
    """
    Render the comment list (as returned by ``{% get_xtdcomment_list %}``)
    through the ``comments/list.html`` template.

    Syntax::

        {% render_xtdcomment_list for [object] [...] %}
        {% render_xtdcomment_list for [app].[model] [obj_id] [...] %}
        {% render_xtdcomment_list for ... [using <tmpl>] %}

    Example usage::

        {% render_xtdcomment_list for post %}

    """
    return RenderXtdCommentListNode.handle_token(parser, token)


# ----------------------------------------------------------------------
class GetCommentBoxPropsNode(Node):
    def __init__(self, obj):
        self.obj = Variable(obj)

    def render(self, context):
        obj = self.obj.resolve(context)
        user = context.get('user', None)
        request = context.get('request', None)
        props = frontend.commentbox_props(obj, user, request=request)
        return json.dumps(props)


@register.tag
def get_commentbox_props(parser, token):
    """
    Returns a JSON object with the initial props for the CommentBox component.

    See api.frontend.commentbox_props for full details on the props.
    """
    try:
        tag_name, args = token.contents.split(None, 1)
    except ValueError:
        raise TemplateSyntaxError("%s tag requires arguments" %
                                  token.contents.split()[0])
    match = re.search(r'for (\w+)', args)
    if not match:
        raise TemplateSyntaxError("%s tag had invalid arguments" % tag_name)
    obj = match.groups()[0]
    return GetCommentBoxPropsNode(obj)


# ----------------------------------------------------------------------
@register.simple_tag
def comment_reaction_form_target(comment):
    """
    Get the target URL for the comment reaction form.

    Example::

        <form action="{% comment_reaction_form_target comment %}" method="post">
    """
    return reverse("comments-xtd-react", args=(comment.id,))


@register.inclusion_tag('includes/django_comments_xtd/reactions_buttons.html')
def render_reactions_buttons(user_reactions):
    return {
        'reactions': get_reactions_enum(),
        'user_reactions': user_reactions,
        'break_every': settings.COMMENTS_XTD_REACTIONS_ROW_LENGTH
    }


@register.simple_tag
def reactions_enum_strlist():
    """
    Returns a string representing the list of available comment reactions.

    Each reaction is a comma-separated list of 3 items: the ID of the
    reaction, the name, and the HTML code to represent it as a button.
    By default there are 4 reactions represented by emoji characters. Read
    the docs to know how to extend comment reactions.
    """
    return get_reactions_enum().strlist()


@register.filter
def authors_list(cmt_reaction):
    return [settings.COMMENTS_XTD_API_USER_REPR(author)
            for author in cmt_reaction.authors.all()]


@register.filter
def get_reaction_enum(cmt_reaction):
    """Returns the ReactionEnum corresponding to the given CommentReaction."""
    return get_reactions_enum()(cmt_reaction.reaction)


# ----------------------------------------------------------------------
@register.simple_tag(takes_context=True)
def comment_css_thread_range(context, comment, prefix="l"):
    """
    Returns a string of CSS selectors that render vertical lines to represent
    comment threads. When comment level matches the max_thread_level there is
    no vertical line, as comments in the max_thread_level can not receive
    replies.

    Returns a concatenated string of f'{prefix}{i}' for i in range(level + 1).
    When the given comment has level=2, and the maximum thread level is 2:

        `{% comment_css_thread_range comment %}`

    produces the string: "l0-mid l1-mid l2".
    """
    max_thread_level = context.get('max_thread_level', None)
    if not max_thread_level:
        ctype = ContentType.objects.get_for_model(comment.content_object)
        max_thread_level = max_thread_level_for_content_type(ctype)

    result = ""
    for i in range(comment.level + 1):
        if i == comment.level:
            if comment.level == max_thread_level:
                result += f"{prefix}{i} "
            else:
                result += f"{prefix}{i}-ini "
        else:
            result += f"{prefix}{i}-mid "
    return result.rstrip()


@register.filter(is_safe=True)
def reply_css_thread_range(level, prefix="l"):
    """
    Returns a string of CSS selectors that render vertical lines to represent
    comment threads. When comment level matches the max_thread_level there is
    no vertical line, as comments in the max_thread_level can not receive
    replies.

    Returns a concatenated string of f'{prefix}{i}' for i in range(level + 1).
    If the given comment object has level=1, using the filter as:

        `{{ comment.level|comment_reply_css_thread_range }}`

    produces the string: "l0 l1".
    """
    result = ""
    for i in range(level + 1):
        result += f"{prefix}{i} "
    return mark_safe(result.rstrip())


@register.filter(is_safe=True)
def indent_divs(level, prefix="level-"):
    """
    Returns a concatenated string of "<div class="{prefix}{i}"></div>"
    for i in range(1, level + 1).

    When called as {{ 2|indent_divs }} produces the string:

        '<div class="level-1"></div>
         <div class="level-2"></div>'
    """
    result = ""
    for i in range(1, level + 1):
        result += f'<div class="{prefix}{i}"></div>'
    return mark_safe(result)


@register.filter(is_safe=True)
def hline_div(level, prefix="line-"):
    """
    Returns a DIV that renders a horizontal line connecting the vertical
    comment thread line with the comment reply box.

    When called as {{ comment.level|hline_div }} produces the string:

        '<div class="line-{comment.level}"></div>'
    """
    return mark_safe(f'<div class="{prefix}{level}"></div>')


@register.filter
def get_top_comment(reply_stack):
    return reply_stack[-1]


@register.filter
def pop_comments_gte(reply_stack, level_lte=0):
    comments_lte = []
    try:
        for index in range(len(reply_stack) - 1, -1, -1):
            if reply_stack[index].level < level_lte:
                break
            comments_lte.append(reply_stack.pop(index))
    finally:
        return comments_lte


@register.simple_tag(takes_context=True)
def push_comment(context, comment):
    context['reply_stack'].append(comment)
    return ""


@register.filter
def get_comment(comment_id: str):
    return get_comment_model().objects.get(pk=int(comment_id))


# ----------------------------------------------------------------------
@register.inclusion_tag('django_comments_xtd/only_users_can_post.html')
def render_only_users_can_post_template(object):
    return {'html_id_suffix': get_html_id_suffix(object)}
