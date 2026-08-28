import inspect
import os
import re

import pytest

from mylar import navmenu


@pytest.mark.unit
def test_menu_payload_has_both_groups():
    payload = navmenu.menu_payload()
    assert set(payload.keys()) == {'articles', 'actions'}
    assert len(payload['articles']) > 0
    assert len(payload['actions']) > 0


@pytest.mark.unit
def test_menu_entries_have_expected_shape():
    payload = navmenu.menu_payload()
    for group in ('articles', 'actions'):
        for entry in payload[group]:
            assert set(entry.keys()) == {'label', 'endpoint', 'params', 'kind', 'confirm'}
            assert isinstance(entry['label'], str) and entry['label']
            assert isinstance(entry['endpoint'], str) and entry['endpoint']
            assert isinstance(entry['params'], dict)
            assert entry['kind'] in ('page', 'task')
            assert isinstance(entry['confirm'], bool)


@pytest.mark.unit
def test_articles_are_pages_that_never_require_confirmation():
    payload = navmenu.menu_payload()
    assert all(entry['kind'] == 'page' for entry in payload['articles'])
    assert all(entry['confirm'] is False for entry in payload['articles'])


@pytest.mark.unit
@pytest.mark.parametrize("group", ['articles', 'actions'])
def test_labels_are_unique_within_group(group):
    labels = [entry['label'] for entry in navmenu.menu_payload()[group]]
    assert len(labels) == len(set(labels))


@pytest.mark.unit
def test_payload_is_a_copy_not_shared_state():
    first = navmenu.menu_payload()
    first['articles'].append(
        {'label': 'x', 'endpoint': 'x', 'params': {}, 'kind': 'page', 'confirm': False}
    )
    first['articles'][0]['label'] = 'mutated'
    second = navmenu.menu_payload()
    assert len(second['articles']) == len(first['articles']) - 1
    assert second['articles'][0]['label'] != 'mutated'


def _all_endpoints():
    payload = navmenu.menu_payload()
    return [e['endpoint'] for e in payload['articles'] + payload['actions']]


@pytest.mark.unit
def test_every_endpoint_is_exposed_on_webinterface():
    from mylar import webserve
    for endpoint in _all_endpoints():
        handler = getattr(webserve.WebInterface, endpoint, None)
        assert handler is not None, '%s is not a WebInterface attribute' % endpoint
        assert getattr(handler, 'exposed', False) is True, '%s is not exposed' % endpoint


@pytest.mark.unit
def test_every_endpoint_is_callable_with_no_arguments():
    from mylar import webserve
    for endpoint in _all_endpoints():
        handler = getattr(webserve.WebInterface, endpoint)
        for name, param in inspect.signature(handler).parameters.items():
            if name == 'self':
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            assert param.default is not param.empty, (
                '%s requires argument %s' % (endpoint, name)
            )


def _renders_something_a_browser_can_show(source):
    if 'serve_download' in source:
        return True
    if re.search(r'serve_template\(templatename', source):
        return True
    # a commented-out redirect is not a redirect
    stripped = '\n'.join(
        line for line in source.splitlines() if not line.strip().startswith('#')
    )
    return 'raise cherrypy.HTTPRedirect' in stripped


@pytest.mark.unit
def test_page_entries_actually_render_a_page():
    """A 'page' entry becomes an <a href>, so the endpoint has to render or
    redirect.  Several JSON-only handlers look like pages by their name alone
    (manageNotifs, blockProviders); linking to one dumps raw JSON at the user.
    """
    from mylar import webserve
    payload = navmenu.menu_payload()
    for entry in payload['articles'] + payload['actions']:
        if entry['kind'] != 'page':
            continue
        source = inspect.getsource(getattr(webserve.WebInterface, entry['endpoint']))
        assert _renders_something_a_browser_can_show(source), (
            '%s is listed as a page but renders nothing a browser can show'
            % entry['endpoint']
        )


@pytest.mark.unit
def test_task_entries_are_not_page_renderers():
    """The inverse guard: anything that renders a full page should be linked,
    not fired over ajax and thrown away.
    """
    from mylar import webserve
    for entry in navmenu.menu_payload()['actions']:
        if entry['kind'] != 'task':
            continue
        source = inspect.getsource(getattr(webserve.WebInterface, entry['endpoint']))
        assert not re.search(r'serve_template\(templatename', source), (
            '%s renders a page and should be a link, not a task' % entry['endpoint']
        )


@pytest.mark.unit
def test_page_entries_have_a_template_that_exists():
    """serve_template() swallows a missing template and renders a Mako
    traceback page, so a broken link looks fine until someone clicks it.
    idirectory() is exactly this: it serves idirectory.html, which ships
    nowhere in the tree.
    """
    from mylar import webserve
    template_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'interfaces', 'default'
    )
    for entry in navmenu.menu_payload()['articles'] + navmenu.menu_payload()['actions']:
        if entry['kind'] != 'page':
            continue
        source = inspect.getsource(getattr(webserve.WebInterface, entry['endpoint']))
        for template in re.findall(r'templatename="([^"]+)"', source):
            assert os.path.exists(os.path.join(template_dir, template)), (
                '%s serves %s, which does not exist' % (entry['endpoint'], template)
            )


@pytest.mark.unit
def test_kwargs_only_handlers_get_the_keys_they_index():
    """A handler declared as (self, **kwargs) that then does kwargs['status']
    has a required argument no signature inspection can see -- manageIssues()
    returns a 500 without it.  Any such key must be supplied via 'params'.
    """
    from mylar import webserve
    payload = navmenu.menu_payload()
    for entry in payload['articles'] + payload['actions']:
        source = inspect.getsource(getattr(webserve.WebInterface, entry['endpoint']))
        # only the handler's own body, not any nested helper it happens to contain
        required = set(re.findall(r"kwargs\[['\"](\w+)['\"]\]", source))
        missing = required - set(entry['params'].keys())
        assert not missing, (
            '%s indexes kwargs%s but the menu supplies %s'
            % (entry['endpoint'], sorted(missing), sorted(entry['params']))
        )
