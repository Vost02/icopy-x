# -*- coding: utf-8 -*-
"""Plugin translation packs.

Plugins write their UI in English (manifest name, ui.json, strings handed
to the host API).  A plugin may ship ``lang/<code>.json`` packs next to its
code, keyed by the exact English text; ``tools/plugin_i18n.py`` generates
the ``en.json`` template and checks coverage.  At runtime a string resolves
through the plugin's own pack, then the core pack (by English value), then
falls back to itself.

Covered here:
  - resources.tr_plugin layering and no-op cases
  - plugin_loader.load_translations parsing and error tolerance
  - JsonRenderer translating templates and string state values
  - PluginActivity end to end: title, buttons, lines, set_var, toast, host.tr
  - Plugins menu and promoted main-menu entries, incl. re-localise on resume
  - Every bundled plugin ships complete French and Chinese packs, the
    templates are current, and the packs reach the IPK

All tests run headless via MockCanvas.
"""

import importlib.util
import json
import os

import pytest

from tests.ui.conftest import MockCanvas
import actstack
import resources
import actmain
from _constants import KEY_OK, KEY_M2
from plugin_loader import PluginInfo, load_translations, _load_single_plugin
from plugin_activity import PluginActivity
from plugins_menu import PluginsMenuActivity
from json_renderer import JsonRenderer


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_PLUGINS_DIR = os.path.join(_REPO_ROOT, 'plugins')
_TOOLS_DIR = os.path.join(_REPO_ROOT, 'tools')


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin_i18n = _load_module('plugin_i18n', os.path.join(_TOOLS_DIR, 'plugin_i18n.py'))


# =====================================================================
# Test data
# =====================================================================

FR = {
    'Test Plugin': 'Extension test',
    'Hello': 'Bonjour',
    'Found: {tag_type}': 'Trouvé : {tag_type}',
    'Clone': 'Cloner',
    'No tag detected': 'Aucun tag détecté',
    'Clone failed: %s': 'Échec du clonage : %s',
}
TRANSLATIONS = {'fr': FR}

TEST_UI = {
    'initial_state': 'main',
    'states': {
        'main': {
            'screen': {
                'title': 'Test Plugin',
                'content': {'type': 'text', 'lines': [
                    {'text': 'Hello'},
                    {'text': 'Found: {tag_type}'},
                    {'text': '{error_msg}'},
                ]},
                'buttons': {'left': 'Back', 'right': 'Clone'},
                'keys': {'M1': 'finish', 'OK': 'set_state:second'},
            },
        },
        'second': {
            'screen': {
                'title': 'Test Plugin',
                'content': {'type': 'text', 'lines': [{'text': 'Hello'}]},
                'buttons': {'left': 'Back', 'right': None},
                'keys': {'M1': 'set_state:main'},
            },
        },
    },
}


def _bundle(translations=TRANSLATIONS, ui=TEST_UI):
    return {
        'plugin_dir': '/tmp/test_plugin',
        'manifest': {'name': 'Test Plugin', 'version': '1.0.0', 'permissions': []},
        'ui_definition': ui,
        'entry_class': None,
        'plugin_key': 'test_plugin',
        'translations': translations,
    }


def _make_plugin_info(name, key, promoted=False, translations=None):
    return PluginInfo(
        name=name, version='1.0.0', author='', description='', key=key,
        plugin_dir='/tmp/' + key, promoted=promoted, canvas_mode=False,
        fullscreen=False, order=100, permissions=[], icon_path=None,
        entry_class_name='X', activity_class=type('X', (), {}),
        manifest={'name': name, 'version': '1.0.0'}, ui_definition=None,
        key_map=None, binary=None, args=[], translations=translations,
    )


class FakeToast(object):
    def __init__(self):
        self.shown = []
        self._showing = False

    def show(self, text, duration_ms=0, icon=None, **kw):
        self.shown.append(text)
        self._showing = True

    def isShow(self):
        return self._showing

    def cancel(self):
        self._showing = False


@pytest.fixture(autouse=True)
def _env():
    actstack._reset()
    actstack._canvas_factory = lambda: MockCanvas()
    before = resources.getLanguage()
    resources.setLanguage('en')
    yield
    resources.setLanguage(before)
    actstack._reset()


def _texts(act):
    return act.getCanvas().get_all_text()


# =====================================================================
# resources.tr_plugin
# =====================================================================

class TestTrPlugin:
    def test_english_is_a_noop(self):
        assert resources.tr_plugin('Hello', TRANSLATIONS) == 'Hello'

    def test_plugin_pack_wins(self):
        resources.setLanguage('fr')
        assert resources.tr_plugin('Hello', TRANSLATIONS) == 'Bonjour'

    def test_core_pack_is_the_fallback(self):
        resources.setLanguage('fr')
        back_fr = resources.get_str('back')
        assert back_fr != 'Back'
        assert resources.tr_plugin('Back', TRANSLATIONS) == back_fr

    def test_plugin_pack_overrides_core(self):
        resources.setLanguage('fr')
        packs = {'fr': {'Back': 'Précédent'}}
        assert resources.tr_plugin('Back', packs) == 'Précédent'

    def test_unknown_text_passes_through(self):
        resources.setLanguage('fr')
        assert resources.tr_plugin('EM410x', TRANSLATIONS) == 'EM410x'

    def test_missing_language_in_pack_falls_back(self):
        resources.setLanguage('zh')
        assert resources.tr_plugin('Hello', TRANSLATIONS) == 'Hello'

    def test_none_and_non_string_inputs(self):
        resources.setLanguage('fr')
        assert resources.tr_plugin('Hello', None) == 'Hello'
        assert resources.tr_plugin(None, TRANSLATIONS) is None
        assert resources.tr_plugin(42, TRANSLATIONS) == 42
        assert resources.tr_plugin('', TRANSLATIONS) == ''

    def test_empty_translation_is_ignored(self):
        resources.setLanguage('fr')
        assert resources.tr_plugin('Hello', {'fr': {'Hello': ''}}) == 'Hello'


# =====================================================================
# plugin_loader.load_translations
# =====================================================================

class TestLoadTranslations:
    def _write_plugin(self, tmp_path, packs):
        (tmp_path / 'manifest.json').write_text(json.dumps({
            'name': 'Test Plugin', 'version': '1.0.0', 'entry_class': 'TestPlugin',
        }), encoding='utf-8')
        (tmp_path / 'plugin.py').write_text(
            'class TestPlugin(object):\n'
            '    def __init__(self, host=None):\n'
            '        self.host = host\n', encoding='utf-8')
        if packs is not None:
            lang = tmp_path / 'lang'
            lang.mkdir()
            for fn, content in packs.items():
                (lang / fn).write_text(content, encoding='utf-8')
        return str(tmp_path)

    def test_packs_loaded_and_cleaned(self, tmp_path):
        plugin_dir = self._write_plugin(tmp_path, {
            'en.json': json.dumps({'Hello': 'Hello'}),
            'fr.json': json.dumps({
                '_comment': 'meta', 'Hello': 'Bonjour', 'Bad': 5, 'Empty': '',
            }),
            'notes.txt': 'not a pack',
        })
        packs = load_translations(plugin_dir)
        assert packs == {'fr': {'Hello': 'Bonjour'}}

    def test_malformed_pack_is_skipped_not_fatal(self, tmp_path):
        plugin_dir = self._write_plugin(tmp_path, {
            'fr.json': json.dumps({'Hello': 'Bonjour'}),
            'zh.json': '{not json',
            'de.json': json.dumps(['a', 'list']),
        })
        assert load_translations(plugin_dir) == {'fr': {'Hello': 'Bonjour'}}
        info = _load_single_plugin(plugin_dir)
        assert info is not None
        assert info.translations == {'fr': {'Hello': 'Bonjour'}}

    def test_no_lang_dir(self, tmp_path):
        plugin_dir = self._write_plugin(tmp_path, None)
        assert load_translations(plugin_dir) == {}
        info = _load_single_plugin(plugin_dir)
        assert info.translations == {}


# =====================================================================
# JsonRenderer.resolve with a translator
# =====================================================================

class TestRendererTranslation:
    def _renderer(self, translate=None):
        r = JsonRenderer(MockCanvas())
        r.set_state({'tag_type': 'EM410x', 'error_msg': 'No tag detected'})
        if translate is not None:
            r.set_translator(translate)
        return r

    def test_without_translator_only_placeholders_are_filled(self):
        r = self._renderer()
        assert r.resolve('Found: {tag_type}') == 'Found: EM410x'
        assert r.resolve('Hello') == 'Hello'

    def test_template_is_translated_before_formatting(self):
        resources.setLanguage('fr')
        r = self._renderer(lambda t: resources.tr_plugin(t, TRANSLATIONS))
        assert r.resolve('Found: {tag_type}') == 'Trouvé : EM410x'
        assert r.resolve('Hello') == 'Bonjour'

    def test_string_state_values_are_translated_state_stays_english(self):
        resources.setLanguage('fr')
        r = self._renderer(lambda t: resources.tr_plugin(t, TRANSLATIONS))
        assert r.resolve('{error_msg}') == 'Aucun tag détecté'
        assert r.state['error_msg'] == 'No tag detected'

    def test_non_string_inputs_pass_through(self):
        resources.setLanguage('fr')
        r = self._renderer(lambda t: resources.tr_plugin(t, TRANSLATIONS))
        assert r.resolve(None) is None
        assert r.resolve('') == ''
        assert r.resolve(7) == 7

    def test_translator_can_be_removed(self):
        resources.setLanguage('fr')
        r = self._renderer(lambda t: resources.tr_plugin(t, TRANSLATIONS))
        r.set_translator(None)
        assert r.resolve('Hello') == 'Hello'


# =====================================================================
# PluginActivity end to end
# =====================================================================

class TestPluginActivityTranslation:
    def test_english_screen_unchanged(self):
        act = actstack.start_activity(PluginActivity, _bundle())
        texts = _texts(act)
        assert 'Test Plugin' in texts
        assert 'Hello' in texts
        assert 'Back' in texts and 'Clone' in texts

    def test_french_title_lines_and_buttons(self):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginActivity, _bundle())
        texts = _texts(act)
        assert 'Extension test' in texts          # manifest name via plugin pack
        assert 'Bonjour' in texts                 # ui.json line via plugin pack
        assert 'Cloner' in texts                  # button via plugin pack
        assert resources.get_str('back') in texts  # button via core fallback
        assert 'Hello' not in texts and 'Clone' not in texts and 'Back' not in texts

    def test_set_var_values_are_localised_but_state_stays_english(self):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginActivity, _bundle())
        act.set_var('tag_type', 'EM410x')
        act.set_var('error_msg', 'No tag detected')
        act.update_screen()
        texts = _texts(act)
        assert 'Trouvé : EM410x' in texts
        assert 'Aucun tag détecté' in texts
        assert act.get_var('error_msg') == 'No tag detected'

    def test_state_change_keeps_translating(self):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginActivity, _bundle())
        act.callKeyEvent(KEY_OK)
        assert act._current_state_id == 'second'
        assert 'Bonjour' in _texts(act)

    def test_host_tr_and_toast(self):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginActivity, _bundle())
        assert act.tr('Clone failed: %s') % 'x' == 'Échec du clonage : x'
        assert act.tr('EM410x') == 'EM410x'
        toast = FakeToast()
        act._toast = toast
        act.show_toast('No tag detected')
        assert toast.shown == ['Aucun tag détecté']

    def test_without_packs_falls_back_to_core_then_english(self):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginActivity, _bundle(translations=None))
        texts = _texts(act)
        assert 'Hello' in texts
        assert resources.get_str('back') in texts

    def test_inactive_button_flag_survives(self):
        resources.setLanguage('fr')
        ui = json.loads(json.dumps(TEST_UI))
        ui['states']['main']['screen']['buttons']['right'] = {
            'text': 'Clone', 'active': False}
        ui['states']['main']['screen']['keys']['M2'] = 'set_state:second'
        act = actstack.start_activity(PluginActivity, _bundle(ui=ui))
        assert 'Cloner' in _texts(act)
        act.callKeyEvent(KEY_M2)
        assert act._current_state_id == 'main'


# =====================================================================
# Menus
# =====================================================================

class TestPluginsMenu:
    @pytest.fixture
    def plugins(self, monkeypatch):
        infos = [
            _make_plugin_info('Test Plugin', 'test_plugin', translations=TRANSLATIONS),
            _make_plugin_info('Other', 'other'),
        ]
        monkeypatch.setattr(actmain, '_discovered_plugins', infos)
        return infos

    def test_names_localised(self, plugins):
        resources.setLanguage('fr')
        act = actstack.start_activity(PluginsMenuActivity)
        texts = _texts(act)
        assert 'Extension test' in texts
        assert 'Other' in texts
        assert 'Test Plugin' not in texts

    def test_relocalises_on_resume_keeping_selection(self, plugins):
        act = actstack.start_activity(PluginsMenuActivity)
        assert 'Test Plugin' in _texts(act)
        act.lv_plugins.setSelection(1)
        resources.setLanguage('fr')
        act.onResume()
        assert 'Extension test' in _texts(act)
        assert act.lv_plugins.selection() == 1
        resources.setLanguage('en')
        act.onResume()
        assert 'Test Plugin' in _texts(act)

    def test_bundle_carries_translations(self, plugins, monkeypatch):
        started = []
        monkeypatch.setattr(actstack, 'start_activity',
                            lambda cls, bundle=None: started.append(bundle))
        act = PluginsMenuActivity()
        act._plugins = plugins
        act._launchPlugin(0)
        assert started and started[0]['translations'] == TRANSLATIONS


class TestMainMenuPromotedPlugin:
    def test_promoted_entry_uses_plugin_pack(self, monkeypatch):
        monkeypatch.setattr(actmain, '_discovered_plugins', [
            _make_plugin_info('Test Plugin', 'test_plugin', promoted=True,
                              translations=TRANSLATIONS),
        ])
        resources.setLanguage('fr')
        act = actstack.start_activity(actmain.MainActivity)
        # The promoted entry sits just before Settings, past the first
        # page: select it so the ListView draws that page.
        idx = len(act._menu_items) - 2
        assert act._menu_items[idx][2] == 'plugin:test_plugin'
        act.lv_main_page.setSelection(idx)
        assert act.lv_main_page.getSelection() == 'Extension test'
        assert 'Extension test' in _texts(act)
        resources.setLanguage('en')
        act.onResume()
        assert act.lv_main_page.getSelection() == 'Test Plugin'
        assert 'Test Plugin' in _texts(act)


# =====================================================================
# Bundled plugins ship complete packs
# =====================================================================

def _bundled():
    return [os.path.basename(d) for d in plugin_i18n.bundled_plugin_dirs(_PLUGINS_DIR)]


class TestBundledPacks:
    @pytest.mark.parametrize('plugin', _bundled())
    def test_packs_complete_and_templates_current(self, plugin):
        errors, _warnings = plugin_i18n.check(
            os.path.join(_PLUGINS_DIR, plugin), ['fr', 'zh'], require=True)
        assert errors == []

    @pytest.mark.parametrize('plugin', _bundled())
    def test_no_untranslatable_runtime_strings(self, plugin):
        _strings, warnings = plugin_i18n.extract_strings(
            os.path.join(_PLUGINS_DIR, plugin))
        assert warnings == []

    @pytest.mark.parametrize('plugin', _bundled())
    def test_packs_are_translated(self, plugin):
        """Values must differ from English, bar token-only lines like "B0: {blk0}"."""
        plugin_dir = os.path.join(_PLUGINS_DIR, plugin)
        own, _strings, _ = plugin_i18n.required_strings(plugin_dir)
        for code in ('fr', 'zh'):
            pack = plugin_i18n.load_pack(plugin_i18n.pack_path(plugin_dir, code))
            same = [s for s in own if pack.get(s) == s]
            assert len(same) <= max(2, len(own) // 2), (code, same)

    def test_check_cli_passes_for_all_bundled_plugins(self, capsys):
        assert plugin_i18n.main(['check', '--require']) == 0

    def test_core_pack_has_shared_plugin_labels(self):
        for label in ('Again', 'Confirm', 'Menu', 'Next', 'Done', 'Exit'):
            for code in ('fr', 'zh'):
                resources.setLanguage(code)
                assert resources.tr(label), (code, label)
        resources.setLanguage('zh')
        assert resources.tr('Again') != 'Again'

    def test_packs_are_packaged_into_the_ipk(self):
        build_ipk = _load_module('build_ipk', os.path.join(_TOOLS_DIR, 'build_ipk.py'))
        ipk_paths = {p.replace('\\', '/')
                     for _s, p in build_ipk.collect_plugins(_PLUGINS_DIR)}
        for plugin in _bundled():
            for code in ('en', 'fr', 'zh'):
                assert 'plugins/%s/lang/%s.json' % (plugin, code) in ipk_paths
