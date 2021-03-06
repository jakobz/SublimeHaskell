import json
import os
import re
import sublime
import sublime_plugin
import subprocess
import threading
import time

from sublime_haskell_common import PACKAGE_PATH, get_setting, get_setting_async, get_cabal_project_dir_of_file, get_cabal_project_dir_of_view, call_and_wait, call_ghcmod_and_wait, log, wait_for_window, output_error, get_settings, attach_sandbox, is_enabled_haskell_command, get_cabal_in_dir

# Completion text longer than this is ellipsized:
MAX_COMPLETION_LENGTH = 37

# If true, files that have not changed will not be re-inspected.
CHECK_MTIME = True

MODULE_INSPECTOR_SOURCE_PATH = os.path.join(PACKAGE_PATH, 'ModuleInspector.hs')
MODULE_INSPECTOR_EXE_PATH = os.path.join(PACKAGE_PATH, 'ModuleInspector')
MODULE_INSPECTOR_OBJ_DIR = os.path.join(PACKAGE_PATH, 'obj')
CABAL_INSPECTOR_SOURCE_PATH = os.path.join(PACKAGE_PATH, 'CabalInspector.hs')
CABAL_INSPECTOR_EXE_PATH = os.path.join(PACKAGE_PATH, 'CabalInspector')
CABAL_INSPECTOR_OBJ_DIR = os.path.join(PACKAGE_PATH, 'obj')

OUTPUT_PATH = os.path.join(PACKAGE_PATH, 'module_info.cache')

# The agent sleeps this long between inspections.
AGENT_SLEEP_DURATION = 5.0

# Checks if we are in a LANGUAGE pragma.
LANGUAGE_RE = re.compile(r'.*{-#\s+LANGUAGE.*')

# Checks if we are in an import statement.
IMPORT_RE = re.compile(r'.*import(\s+qualified)?\s+')
IMPORT_RE_PREFIX = re.compile(r'^\s*import(\s+qualified)?\s+(.*)$')
IMPORT_QUALIFIED_POSSIBLE_RE = re.compile(r'.*import\s+(?P<qualifiedprefix>\S*)$')

# Checks if a word contains only alhanums, -, and _
NO_SPECIAL_CHARS_RE = re.compile(r'^(\w|[\-])*$')


def get_line_contents(view, location):
    """
    Returns the contents of the line at the given location.
    """
    return view.substr(sublime.Region(view.line(location).a, location))

# Autocompletion data
class AutoCompletion(object):
    """Information for completion"""
    def __init__(self):
        self.language_completions = []
        self.module_completions = []
        # Module info (dictionary: filename => info)
        # info is:
        #   moduleName - name of module
        #   exportList - list of export (strings)
        #   imports - list of import (strings), where import is:
        #     importName - name of imported module
        #     qualified - is import qualified?
        #     as - alias of module (string or null)
        #   declarations - list of declarations, where declaration is:
        #     info - type info (string "(data)", "(type)" or "(class)")
        #     identifier - declaration identifier
        self.info_lock = threading.Lock()
        self.info = {}
        # Standard module completions (dictionary: module name => completions):
        self.std_info_lock = threading.Lock()
        self.std_info = {}

        # Currently used projects
        # name => project where project is:
        #   dir - project dir
        #   cabal - cabal file
        #   executables - list of executables where executable is
        #     name - name of executable
        self.projects_lock = threading.Lock()
        self.projects = {}

    def get_completions(self, view, prefix, locations):
        "Get all the completions that apply to the current file."

        line_contents = get_line_contents(view, locations[0])

        # 'bla bla bla Data.List.fo' -> ('Data.List', 'Data.List.fo')
        def get_qualify(s):
            if len(s) == 0:
                return ('', '')
            quals = s.split()[-1].split('.')
            filtered = map(lambda s: filter(lambda c: c.isalpha(), s), quals)
            return ('.'.join(filtered[0:len(filtered) - 1]), '.'.join(filtered))

        current_file_name = view.file_name()

        (qualified_module, qualified_prefix) = get_qualify(line_contents)
        has_q = len(qualified_module) != 0

        completions = []

        # Complete with modules too
        if has_q:
            completions.extend(self.get_module_completions_for(qualified_prefix))

        with self.info_lock:

            moduleImports = []
            # Use completion only from qualified_module
            if has_q:
                if current_file_name in self.info:
                    current_info = self.info[current_file_name]
                    if 'imports' in current_info:
                        # if qualified_module is alias, find its original name
                        # e.g. for 'import Data.Text as T' return 'Data.Text' for 'T'
                        moduleImports.extend([m['importName'] for m in self.info[current_file_name]['imports'] if m['as'] == qualified_module])
                moduleImports.append(qualified_module)
            else:
                # list of imports, imported unqualified
                if current_file_name in self.info:
                    current_info = self.info[current_file_name]
                    if 'imports' in current_info:
                        moduleImports.extend([m['importName'] for m in self.info[current_file_name]['imports'] if not m['qualified']])

            for file_name, file_info in self.info.items():
                if 'error' in file_info:
                    # There was an error parsing this file; skip it
                    continue

                # File is imported, add to completion list
                if file_info['moduleName'] in moduleImports:
                    for d in file_info['declarations']:
                        identifier = d['identifier']
                        declaration_info = d['info']
                        # TODO: Show the declaration info somewhere.
                        completions.append((identifier[:MAX_COMPLETION_LENGTH], identifier))

            # Completion for modules by ghc-mod browse
            with self.std_info_lock:
                for mi in moduleImports:
                    if mi not in self.std_info:
                        # Module not imported, skip it
                        continue

                    std_module = self.std_info[mi]

                    for v in std_module:
                        completions.append((v[:MAX_COMPLETION_LENGTH], v))

        return list(set(completions))

    def get_import_completions(self, view, prefix, locations):

        # Contents of the current line up to the cursor
        line_contents = get_line_contents(view, locations[0])

        # Autocompletion for LANGUAGE pragmas
        if get_setting('auto_complete_language_pragmas'):
            # TODO handle multiple selections
            match_language = LANGUAGE_RE.match(line_contents)
            if match_language:
                return [ (unicode(c),) * 2 for c in self.language_completions ]

        # Autocompletion for import statements
        if get_setting('auto_complete_imports'):
            match_import = IMPORT_RE_PREFIX.match(line_contents)
            if match_import:
                (qualified, pref) = match_import.groups()
                import_completions = self.get_module_completions_for(pref)

                # Right after "import "? Propose "qualified" as well!
                qualified_match = IMPORT_QUALIFIED_POSSIBLE_RE.match(line_contents)
                if qualified_match:
                    qualified_prefix = qualified_match.group('qualifiedprefix')
                    if qualified_prefix == "" or "qualified".startswith(qualified_prefix):
                        import_completions.insert(0, (u"qualified", "qualified "))

                return list(set(import_completions))

        return None

    def get_module_completions_for(self, qualified_prefix):
        def module_next_name(mname):
            """
            Returns next name for prefix
            pref = Control.Con, mname = Control.Concurrent.MVar, result = Concurrent
            """
            return mname.split('.')[len(qualified_prefix.split('.')) - 1]
        return list(set([ (unicode(module_next_name(m)),) * 2 for m in self.module_completions if m.startswith(qualified_prefix) ]))


autocompletion = AutoCompletion()

class SublimeHaskellBrowseDeclarations(sublime_plugin.WindowCommand):
    def run(self):
        self.names = []
        self.declarations = []
        for f, v in autocompletion.info.items():
            if 'declarations' in v:
                for d in v['declarations']:
                    self.names.append(d['identifier'])
                    self.declarations.append(v['moduleName'] + ': '  + d['identifier'] + ' ' + d['info'])
        for m, decls in autocompletion.std_info.items():
            for decl in decls:
                self.names.append(decl)
                self.declarations.append(m + ': ' + decl)

        self.window.show_quick_panel(self.declarations, self.on_done)

    def on_done(self, idx):
        if idx == -1:
            return
        view = self.window.active_view()
        if not view:
            return
        edit = view.begin_edit()
        for r in view.sel():
            view.replace(edit, r, self.names[idx])
        view.end_edit(edit)

    def is_enabled(self):
        return is_enabled_haskell_command(False)

class SublimeHaskellGoToAnyDeclaration(sublime_plugin.WindowCommand):
    def run(self):
        self.files = []
        self.declarations = []
        for f, v in autocompletion.info.items():
            if 'declarations' in v:
                for d in v['declarations']:
                    self.files.append([f, str(d['line']), str(d['column'])])
                    self.declarations.append([d['identifier'] + ' ' + d['info'], v['moduleName'] + ':' + str(d['line']) + ':' + str(d['column'])])
        self.window.show_quick_panel(self.declarations, self.on_done)

    def on_done(self, idx):
        if idx == -1:
            return
        self.window.open_file(':'.join(self.files[idx]), sublime.ENCODED_POSITION)

    def is_enabled(self):
        return is_enabled_haskell_command(False)

class SublimeHaskellGoToDeclaration(sublime_plugin.TextCommand):
    def run(self, edit):
        ident = self.view.substr(self.view.word(self.view.sel()[0]))
        for f, v in autocompletion.info.items():
            if 'declarations' in v:
                for d in v['declarations']:
                    if d['identifier'] == ident:
                        self.view.window().open_file(':'.join([f, str(d['line']), str(d['column'])]), sublime.ENCODED_POSITION)
                        return

    def is_enabled(self):
        return is_enabled_haskell_command(False)

class SublimeHaskellAutocomplete(sublime_plugin.EventListener):
    def __init__(self):
        # TODO: Start the InspectorAgent as a separate thread.
        self.inspector = InspectorAgent()
        self.inspector.start()

        autocompletion.language_completions = []
        autocompletion.module_completions = []

        self.local_settings = {
            'enable_ghc_mod' : None,
            'use_cabal_dev' : None,
            'cabal_dev_sandbox' : None }

        for s in self.local_settings.keys():
            self.local_settings[s] = get_setting(s)

        self.init_ghcmod_completions()

        # Subscribe to settings changes to update data
        get_settings().add_on_change('enable_ghc_mod', lambda: self.on_setting_changed())

    def on_setting_changed(self):
        same = True
        for k, v in self.local_settings.items():
            r = get_setting(k)
            same = same and v == r
            self.local_settings[k] = r

        if not same:
            self.init_ghcmod_completions()

    # Gets available LANGUAGE options and import modules from ghc-mod
    def init_ghcmod_completions(self):

        if not get_setting('enable_ghc_mod'):
            return

        sublime.status_message('SublimeHaskell: Updating ghc_mod completions...')

        # Init LANGUAGE completions
        autocompletion.language_completions = call_ghcmod_and_wait(['lang']).splitlines()
        log("Reading LANGUAGE completions from ghc-mod")

        # Init import module completion
        autocompletion.module_completions = call_ghcmod_and_wait(['list']).splitlines()

        sublime.status_message('SublimeHaskell: Updating ghc_mod completions ' + u" \u2714")

    def get_special_completions(self, view, prefix, locations):

        # Contents of the current line up to the cursor
        line_contents = get_line_contents(view, locations[0])

        # Autocompletion for LANGUAGE pragmas
        if get_setting('auto_complete_language_pragmas'):
            # TODO handle multiple selections
            match_language = LANGUAGE_RE.match(line_contents)
            if match_language:
                return [ (unicode(c),) * 2 for c in autocompletion.language_completions ]

        # Autocompletion for import statements
        if get_setting('auto_complete_imports'):
            match_import = IMPORT_RE.match(line_contents)
            if match_import:
                import_completions = [ (unicode(c),) * 2 for c in autocompletion.module_completions ]

                # Right after "import "? Propose "qualified" as well!
                qualified_match = IMPORT_QUALIFIED_POSSIBLE_RE.match(line_contents)
                if qualified_match:
                    qualified_prefix = qualified_match.group('qualifiedprefix')
                    if qualified_prefix == "" or "qualified".startswith(qualified_prefix):
                        import_completions.insert(0, (u"qualified", "qualified "))

                return import_completions

        return None

    def on_query_completions(self, view, prefix, locations):
        begin_time = time.clock()
        # Only suggest symbols if the current file is part of a Cabal project.
        # TODO: Only suggest symbols from within this project.

        cabal_dir = get_cabal_project_dir_of_view(view)
        # if cabal_dir is not None:

        completions = autocompletion.get_import_completions(view, prefix, locations)

        if not completions:
            completions = autocompletion.get_completions(view, prefix, locations)

        end_time = time.clock()
        log('time to get completions: {0} seconds'.format(end_time - begin_time))
        # Don't put completions with special characters (?, !, ==, etc.)
        # into completion because that wipes all default Sublime completions:
        # See http://www.sublimetext.com/forum/viewtopic.php?t=8659
        # TODO: work around this
        return [ c for c in completions if NO_SPECIAL_CHARS_RE.match(c[0]) ]

        return []

    def on_new(self, view):
        filename = view.file_name();
        if filename:
            self.inspector.mark_file_dirty(filename)

    def on_post_save(self, view):
        filename = view.file_name()
        if filename:
            self.inspector.mark_file_dirty(filename)

class InspectorAgent(threading.Thread):
    def __init__(self):
        # Call the superclass constructor:
        super(InspectorAgent, self).__init__()
        # Make this thread daemonic so that it won't prevent the program
        # from exiting.
        self.daemon = True
        # Files that need to be re-inspected:
        self.dirty_files_lock = threading.Lock()
        self.dirty_files = []

    def run(self):
        # Compile the CabalInspector:
        # TODO: Where to compile it?
        sublime.set_timeout(lambda: sublime.status_message('Compiling Haskell CabalInspector...'), 0)

        exit_code, out, err = call_and_wait(['ghc',
            '--make', CABAL_INSPECTOR_SOURCE_PATH,
            '-o', CABAL_INSPECTOR_EXE_PATH,
            '-outputdir', CABAL_INSPECTOR_OBJ_DIR])

        if exit_code != 0:
            error_msg = u"SublimeHaskell: Failed to compile CabalInspector\n{0}".format(err)
            wait_for_window(lambda w: self.show_errors(w, error_msg))
        else:
            sublime.set_timeout(lambda: sublime.status_message('Compiling Haskell CabalInspector' + u" \u2714"), 0)
        # Continue anyway

        # Compile the ModuleInspector:
        sublime.set_timeout(lambda: sublime.status_message('Compiling Haskell ModuleInspector...'), 0)

        exit_code, out, err = call_and_wait(['ghc',
            '--make', MODULE_INSPECTOR_SOURCE_PATH,
            '-o', MODULE_INSPECTOR_EXE_PATH,
            '-outputdir', MODULE_INSPECTOR_OBJ_DIR])

        if exit_code != 0:
            error_msg = u"SublimeHaskell: Failed to compile ModuleInspector\n{0}".format(err)
            wait_for_window(lambda w: self.show_errors(w, error_msg))
            return

        sublime.set_timeout(lambda: sublime.status_message('Compiling Haskell ModuleInspector' + u" \u2714"), 0)

        # For first time, inspect all open folders and files
        wait_for_window(lambda w: self.mark_all_files(w))

        # TODO: If compilation failed, we can't proceed; handle this.
        # Periodically wake up and see if there is anything to inspect.
        while True:
            files_to_reinspect = []
            with self.dirty_files_lock:
                files_to_reinspect = self.dirty_files
                self.dirty_files = []
            # Find the cabal project corresponding to each "dirty" file:
            cabal_dirs = []
            standalone_files = []
            for filename in files_to_reinspect:
                d = get_cabal_project_dir_of_file(filename)
                if d is not None:
                    cabal_dirs.append(d)
                else:
                    standalone_files.append(filename)
            # Eliminate duplicate project directories:
            cabal_dirs = list(set(cabal_dirs))
            standalone_files = list(set(standalone_files))
            for d in cabal_dirs:
                self._refresh_all_module_info(d)
            for f in standalone_files:
                self._refresh_module_info(f)
            time.sleep(AGENT_SLEEP_DURATION)

    def mark_all_files(self, window):
        folder_files = []
        for folder in window.folders():
            folder_files.extend(list_files_in_dir_recursively(folder))
        with self.dirty_files_lock:
            self.dirty_files.extend(folder_files)

    def show_errors(self, window, error_text):
        sublime.set_timeout(lambda: sublime.status_message('Compiling Haskell ModuleInspector' + u" \u2717"), 0)
        sublime.set_timeout(lambda: output_error(window, error_text), 0)

    def mark_file_dirty(self, filename):
        "Report that a file should be reinspected."
        with self.dirty_files_lock:
            self.dirty_files.append(filename)

    def _refresh_all_module_info(self, cabal_dir):
        "Rebuild module information for all files under the specified directory."
        begin_time = time.clock()
        log('reinspecting project ({0})'.format(cabal_dir))
        # Process all files within the Cabal project:
        # TODO: Only process files within the .cabal file's "src" directory.
        (project_name, cabal_file) = get_cabal_in_dir(cabal_dir)
        # set project and read cabal
        if cabal_file and project_name:
            self._refresh_project_info(cabal_dir, project_name, cabal_file)

        files_in_dir = list_files_in_dir_recursively(cabal_dir)
        haskell_source_files = [x for x in files_in_dir if x.endswith('.hs') and ('dist/build/autogen' not in x)]
        for filename in haskell_source_files:
            self._refresh_module_info(filename)
        end_time = time.clock()
        log('total inspection time: {0} seconds'.format(end_time - begin_time))

    def _refresh_project_info(self, cabal_dir, project_name, cabal_file):
        exit_code, out, err = call_and_wait(
            [CABAL_INSPECTOR_EXE_PATH, cabal_file])

        if exit_code == 0:
            new_info = json.loads(out)

            if 'error' not in new_info:
                if 'executables' in new_info:
                    with autocompletion.projects_lock:
                        autocompletion.projects[project_name] = {
                            'dir': cabal_dir,
                            'cabal': os.path.basename(cabal_file),
                            'executables': new_info['executables'] }

    def _refresh_module_info(self, filename):
        "Rebuild module information for the specified file."
        # TODO: Only do this within Haskell files in Cabal projects.
        # TODO: Skip this file if it hasn't changed since it was last inspected.
        # TODO: Currently the ModuleInspector only delivers top-level functions
        #       with hand-written type signatures. This code should make that clear.
        # If the file hasn't changed since it was last inspected, do nothing:
        if not filename.endswith('.hs'):
            return

        modification_time = os.stat(filename).st_mtime
        if CHECK_MTIME:
            inspection_time = self._get_inspection_time_of_file(filename)
            if modification_time <= inspection_time:
                return
        exit_code, stdout, stderr = call_and_wait(
            [MODULE_INSPECTOR_EXE_PATH, filename])
        # Update only when module is ok
        if exit_code == 0:
            new_info = json.loads(stdout)

            if 'error' not in new_info:
                # Load standard modules
                if 'imports' in new_info:
                    for mi in new_info['imports']:
                        if 'importName' in mi:
                            self._load_standard_module(mi['importName'])

                # Remember when this info was collected.
                new_info['inspectedAt'] = modification_time
                # Dump the currently-known module info to disk:
                formatted_json = json.dumps(autocompletion.info, indent=2)
                with open(OUTPUT_PATH, 'w') as f:
                    f.write(formatted_json)
                with autocompletion.info_lock:
                    autocompletion.info[filename] = new_info

    def _load_standard_module(self, module_name):
        if module_name not in autocompletion.std_info:
            module_contents = call_ghcmod_and_wait(['browse', module_name]).splitlines()
            with autocompletion.std_info_lock:
                autocompletion.std_info[module_name] = module_contents

    def _get_inspection_time_of_file(self, filename):
        """Return the time that a file was last inspected.
        Return zero if it has never been inspected."""
        with autocompletion.info_lock:
            try:
                return autocompletion.info[filename]['inspectedAt']
            except KeyError:
                return 0.0

def list_files_in_dir_recursively(base_dir):
    """Return a list of a all files in a directory, recursively.
    The files will be specified by full paths."""
    files = []
    for dirname, dirnames, filenames in os.walk(base_dir):
        for filename in filenames:
            files.append(os.path.join(base_dir, dirname, filename))
    return files
