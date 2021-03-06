import logging
import re

from difflib import Differ
from inspect import Parameter
from typing import List, Dict, Optional, Any, Iterator, Callable, Union

from jedi import (Script, create_environment,  # type: ignore
                  get_default_environment,
                  settings as jedi_settings, get_default_project,
                  RefactoringError)
from jedi.api.classes import Name, Completion  # type: ignore
from jedi.api.refactoring import Refactoring, ChangedFile  # type: ignore
from parso import split_lines  # type: ignore

from pycodestyle import (BaseReport as CodestyleBaseReport,  # type: ignore
                         Checker as CodestyleChecker,
                         StyleGuide as CodestyleStyleGuide)

from pyflakes.api import check as pyflakes_check  # type: ignore

from pygls.features import (COMPLETION, TEXT_DOCUMENT_DID_CHANGE,
                            TEXT_DOCUMENT_DID_CLOSE, TEXT_DOCUMENT_DID_OPEN,
                            HOVER, SIGNATURE_HELP, DEFINITION,
                            REFERENCES, WORKSPACE_DID_CHANGE_CONFIGURATION,
                            TEXT_DOCUMENT_WILL_SAVE, TEXT_DOCUMENT_DID_SAVE,
                            DOCUMENT_SYMBOL, CODE_ACTION)
from pygls import types
from pygls.server import LanguageServer
from pygls.protocol import LanguageServerProtocol
from pygls.uris import from_fs_path, to_fs_path

from .version import get_version

RE_WORD = re.compile(r'\w*')


_COMPLETION_TYPES = {
    'module': types.CompletionItemKind.Module,
    'class': types.CompletionItemKind.Class,
    'instance': types.CompletionItemKind.Reference,
    'function': types.CompletionItemKind.Function,
    'param': types.CompletionItemKind.Variable,
    'keyword': types.CompletionItemKind.Keyword,
    'statement': types.CompletionItemKind.Keyword
}


jedi_settings.case_insensitive_completion = False


completionFunction: Callable[[List[Completion], types.Range],
                             Iterator[types.CompletionItem]]
documentSymbolFunction: Union[
    Callable[[str, List[str], List[Name]], List[types.DocumentSymbol]],
    Callable[[str, List[str], List[Name]], List[types.SymbolInformation]]]


class AnakinLanguageServerProtocol(LanguageServerProtocol):

    def bf_initialize(
            self, params: types.InitializeParams) -> types.InitializeResult:
        result = super().bf_initialize(params)
        global jediEnvironment
        global jediProject
        global completionFunction
        global documentSymbolFunction
        venv = getattr(params.initializationOptions, 'venv', None)
        if venv:
            jediEnvironment = create_environment(venv, False)
        else:
            jediEnvironment = get_default_environment()
        jediProject = get_default_project(getattr(params, 'rootPath', None))
        logging.info(f'Jedi environment python: {jediEnvironment.executable}')
        logging.info('Jedi environment sys_path:')
        for p in jediEnvironment.get_sys_path():
            logging.info(f'  {p}')
        logging.info(f'Jedi project path: {jediProject._path}')

        def get_attr(o, *attrs):
            try:
                for attr in attrs:
                    o = getattr(o, attr)
                return o
            except AttributeError:
                return None

        caps = getattr(params.capabilities, 'textDocument', None)

        if get_attr(caps, 'completion', 'completionItem', 'snippetSupport'):
            completionFunction = _completions_snippets
        else:
            completionFunction = _completions

        if get_attr(caps,
                    'documentSymbol', 'hierarchicalDocumentSymbolSupport'):
            documentSymbolFunction = _document_symbol_hierarchy
        else:
            documentSymbolFunction = _document_symbol_plain

        result.capabilities.textDocumentSync = types.TextDocumentSyncOptions(
            open_close=True,
            change=types.TextDocumentSyncKind.INCREMENTAL,
            save=types.SaveOptions()
        )
        result.capabilities.codeActionProvider = types.CodeActionOptions([
            types.CodeActionKind.RefactorInline,
            types.CodeActionKind.RefactorExtract
        ])
        # pygls does not currently support serverInfo of LSP v3.15
        result.serverInfo = {
            'name': 'anakinls',
            'version': get_version(),
        }
        return result


server = LanguageServer(protocol_cls=AnakinLanguageServerProtocol)

scripts: Dict[str, Script] = {}
pycodestyleOptions: Dict[str, Any] = {}
mypyConfigs: Dict[str, str] = {}

jediEnvironment = None
jediProject = None

config = {
    'pyflakes_errors': [
        'UndefinedName'
    ],
    'pycodestyle_config': None,
    'help_on_hover': True,
    'mypy_enabled': False
}

differ = Differ()


def get_script(ls: LanguageServer, uri: str, update: bool = False) -> Script:
    result = None if update else scripts.get(uri)
    if not result:
        document = ls.workspace.get_document(uri)
        result = Script(
            code=document.source,
            path=document.path,
            environment=jediEnvironment,
            project=jediProject
        )
        scripts[uri] = result
    return result


class PyflakesReporter:

    def __init__(self, result, script, errors):
        self.result = result
        self.script = script
        self.errors = errors

    def unexpectedError(self, _filename, msg):
        self.result.append(types.Diagnostic(
            types.Range(types.Position(), types.Position()),
            msg,
            types.DiagnosticSeverity.Error,
            source='pyflakes'
        ))

    def _get_codeline(self, line):
        return self.script._code_lines[line].rstrip('\n\r')

    def syntaxError(self, _filename, msg, lineno, offset, _text):
        line = lineno - 1
        col = offset or 0
        self.result.append(types.Diagnostic(
            types.Range(
                types.Position(line, col),
                types.Position(line, len(self._get_codeline(line)) - col)
            ),
            msg,
            types.DiagnosticSeverity.Error,
            source='pyflakes'
        ))

    def flake(self, message):
        line = message.lineno - 1
        if message.__class__.__name__ in self.errors:
            severity = types.DiagnosticSeverity.Error
        else:
            severity = types.DiagnosticSeverity.Warning
        self.result.append(types.Diagnostic(
            types.Range(
                types.Position(line, message.col),
                types.Position(line, len(self._get_codeline(line)))
            ),
            message.message % message.message_args,
            severity,
            source='pyflakes'
        ))


class CodestyleReport(CodestyleBaseReport):

    def __init__(self, options, result):
        super().__init__(options)
        self.result = result

    def error(self, line_number, offset, text, check):
        code = text[:4]
        if self._ignore_code(code) or code in self.expected:
            return
        line = line_number - 1
        self.result.append(types.Diagnostic(
            types.Range(
                types.Position(line, offset),
                types.Position(line, len(self.lines[line].rstrip('\n\r')))
            ),
            text,
            types.DiagnosticSeverity.Warning,
            code,
            'pycodestyle'
        ))


def _get_workspace_folder_path(ls: LanguageServer, uri: str) -> str:
    # find workspace folder uri belongs to
    folders = sorted(
        (f.uri
         for f in ls.workspace.folders.values()
         if uri.startswith(f.uri)),
        key=len, reverse=True
    )
    if folders:
        return to_fs_path(folders[0])
    return ls.workspace.root_path


def get_pycodestyle_options(ls: LanguageServer, uri: str):
    folder = _get_workspace_folder_path(ls, uri)
    result = pycodestyleOptions.get(folder)
    if not result:
        result = CodestyleStyleGuide(
            paths=[folder],
            config_file=config['pycodestyle_config']
        ).options
        pycodestyleOptions[folder] = result
    return result


def get_mypy_config(ls: LanguageServer, uri: str) -> Optional[str]:
    folder = _get_workspace_folder_path(ls, uri)
    if folder in mypyConfigs:
        return mypyConfigs[folder]
    import os
    from mypy.defaults import CONFIG_FILES
    result = ''
    for filename in CONFIG_FILES:
        filename = os.path.expanduser(filename)
        if not os.path.isabs(filename):
            filename = os.path.join(folder, filename)
        if os.path.exists(filename):
            result = filename
            break
    mypyConfigs[folder] = result
    return result


def _mypy_check(ls: LanguageServer, uri: str, script: Script,
                result: List[types.Diagnostic]):
    from mypy import api
    assert jediEnvironment is not None
    version_info = jediEnvironment.version_info
    filename = to_fs_path(uri)
    lines = api.run([
        '--python-executable', jediEnvironment.executable,
        '--python-version', f'{version_info.major}.{version_info.minor}',
        '--config-file', get_mypy_config(ls, uri),
        '--hide-error-context',
        '--show-column-numbers',
        '--show-error-codes',
        '--no-pretty',
        '--show-absolute-path',
        '--no-error-summary',
        filename
    ])
    if lines[1]:
        ls.show_message(lines[1], types.MessageType.Error)
        return

    for line in lines[0].split('\n'):
        parts = line.split(':', 4)
        if len(parts) < 5:
            continue
        fn, row, column, err_type, message = parts
        if fn != filename:
            continue
        row = int(row) - 1
        column = int(column) - 1
        if err_type.strip() == 'note':
            severity = types.DiagnosticSeverity.Hint
        else:
            severity = types.DiagnosticSeverity.Warning
        result.append(
            types.Diagnostic(
                types.Range(
                    types.Position(row, column),
                    types.Position(row, len(script._code_lines[row]))
                ),
                message.strip(),
                severity,
                source='mypy'
            )
        )
    return result


def _validate(ls: LanguageServer, uri: str):
    # Jedi
    script = get_script(ls, uri)
    result = [
        types.Diagnostic(
            types.Range(
                types.Position(x.line - 1, x.column),
                types.Position(x.until_line - 1, x.until_column)
            ),
            'Invalid syntax',
            types.DiagnosticSeverity.Error,
            source='jedi'
        )
        for x in script.get_syntax_errors()
    ]
    if result:
        ls.publish_diagnostics(uri, result)
        return

    # pyflakes
    pyflakes_check(script._code, script.path,
                   PyflakesReporter(result, script, config['pyflakes_errors']))

    # pycodestyle
    codestyleopts = get_pycodestyle_options(ls, uri)
    CodestyleChecker(
        script.path, script._code.splitlines(True), codestyleopts,
        CodestyleReport(codestyleopts, result)
    ).check_all()

    if config['mypy_enabled']:
        try:
            _mypy_check(ls, uri, script, result)
        except Exception as e:
            ls.show_message(f'mypy check error: {e}',
                            types.MessageType.Warning)

    ls.publish_diagnostics(uri, result)


@server.feature(TEXT_DOCUMENT_DID_OPEN)
def did_open(ls: LanguageServer, params: types.DidOpenTextDocumentParams):
    _validate(ls, params.textDocument.uri)


@server.feature(TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: LanguageServer, params: types.DidCloseTextDocumentParams):
    try:
        del scripts[params.textDocument.uri]
    except KeyError:
        pass


@server.feature(TEXT_DOCUMENT_DID_CHANGE)
def did_change(ls: LanguageServer, params: types.DidChangeTextDocumentParams):
    get_script(ls, params.textDocument.uri, True)


def _completion_sort_key(completion: Completion) -> str:
    name = completion.name
    if name.startswith('__'):
        return f'zz{name}'
    if name.startswith('_'):
        return f'za{name}'
    return f'aa{name}'


def _completion_item(completion: Completion, r: types.Range) -> Dict:
    label = completion.name
    if not completion.complete.startswith("'") and label.startswith("'"):
        label = label[1:]
    return dict(
        label=label,
        kind=_COMPLETION_TYPES.get(completion.type,
                                   types.CompletionItemKind.Text),
        documentation=completion.docstring(raw=True),
        sort_text=_completion_sort_key(completion),
        text_edit=types.TextEdit(r, completion.complete)
    )


def _completions(completions: List[Completion],
                 r: types.Range) -> Iterator[types.CompletionItem]:
    return (
        types.CompletionItem(
            **_completion_item(completion, r)
        ) for completion in completions
    )


def _completions_snippets(completions: List[Completion],
                          r: types.Range) -> Iterator[types.CompletionItem]:
    for completion in completions:
        item = _completion_item(completion, r)
        yield types.CompletionItem(
            **item
        )
        for signature in completion.get_signatures():
            names = []
            snippets = []
            for i, param in enumerate(signature.params):
                if param.kind == Parameter.VAR_KEYWORD:
                    break
                if '=' in param.description:
                    break
                if param.name == '/':
                    continue
                names.append(param.name)
                if param.kind == Parameter.KEYWORD_ONLY:
                    snippet_prefix = f'{param.name}='
                else:
                    snippet_prefix = ''
                snippets.append(
                    f'{snippet_prefix}${{{i + 1}:{param.name}}}'
                )
            names_str = ', '.join(names)
            snippets_str = ', '.join(snippets)
            yield types.CompletionItem(**dict(
                item,
                label=f'{completion.name}({names_str})',
                insert_text=f'{completion.name}({snippets_str})$0',
                insert_text_format=types.InsertTextFormat.Snippet,
                text_edit=None
            ))


@server.feature(COMPLETION, trigger_characters=['.'])
def completions(ls: LanguageServer, params: types.CompletionParams):
    script = get_script(ls, params.textDocument.uri)
    completions = script.complete(
        params.position.line + 1,
        params.position.character
    )
    code_line = script._code_lines[params.position.line]
    word_match = RE_WORD.match(code_line[params.position.character:])
    if word_match:
        word_rest = word_match.end()
    else:
        word_rest = 0
    r = types.Range(
        types.Position(params.position.line,
                       params.position.character),
        types.Position(params.position.line,
                       params.position.character + word_rest)
    )
    return types.CompletionList(False,
                                list(completionFunction(completions, r)))


@server.feature(HOVER)
def hover(ls: LanguageServer,
          params: types.TextDocumentPositionParams) -> Optional[types.Hover]:
    script = get_script(ls, params.textDocument.uri)
    fn = script.help if config['help_on_hover'] else script.infer
    names = fn(params.position.line + 1, params.position.character)
    result = '\n----------\n'.join(x.docstring() for x in names)
    if result:
        return types.Hover(
            types.MarkupContent(types.MarkupKind.PlainText, result)
        )
    return None


@server.feature(SIGNATURE_HELP, trigger_characters=['(', ','])
def signature_help(
        ls: LanguageServer,
        params: types.TextDocumentPositionParams
) -> Optional[types.SignatureHelp]:
    script = get_script(ls, params.textDocument.uri)
    signatures = script.get_signatures(params.position.line + 1,
                                       params.position.character)

    result = []
    idx = -1
    param_idx = -1
    i = 0
    for signature in signatures:
        if signature.index is None:
            continue
        result.append(types.SignatureInformation(
            signature.to_string(),
            parameters=[
                types.ParameterInformation(param.name)
                for param in signature.params
            ]
        ))
        if signature.index > param_idx:
            param_idx = signature.index
            idx = i
        i += 1
    if result:
        return types.SignatureHelp([result[idx]], 0, param_idx)
    return None


def _get_locations(defs: List[Name]) -> List[types.Location]:
    return [
        types.Location(
            from_fs_path(d.module_path),
            types.Range(
                types.Position(d.line - 1, d.column),
                types.Position(d.line - 1, d.column + len(d.name))
            )
        )
        for d in defs if d.module_path
    ]


@server.feature(DEFINITION)
def definition(
        ls: LanguageServer,
        params: types.TextDocumentPositionParams) -> List[types.Location]:
    script = get_script(ls, params.textDocument.uri)
    defs = script.goto(params.position.line + 1, params.position.character)
    return _get_locations(defs)


@server.feature(REFERENCES)
def references(ls: LanguageServer,
               params: types.ReferenceParams) -> List[types.Location]:
    script = get_script(ls, params.textDocument.uri)
    refs = script.get_references(params.position.line + 1,
                                 params.position.character)
    return _get_locations(refs)


@server.feature(WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(ls: LanguageServer,
                             settings: types.DidChangeConfigurationParams):
    if not settings.settings or not hasattr(settings.settings, 'anakinls'):
        return
    changed = set()
    for k in config:
        if hasattr(settings.settings.anakinls, k):
            config[k] = getattr(settings.settings.anakinls, k)
            if k != 'help_on_hover':
                changed.add(k)
    if 'pycodestyle_config' in changed:
        pycodestyleOptions.clear()
    if 'mypy_enabled' in changed:
        mypyConfigs.clear()
    if changed:
        for uri in ls.workspace.documents:
            _validate(ls, uri)


@server.feature(TEXT_DOCUMENT_WILL_SAVE)
def will_save(ls: LanguageServer, params: types.WillSaveTextDocumentParams):
    pass


@server.feature(TEXT_DOCUMENT_DID_SAVE)
def did_save(ls: LanguageServer, params: types.DidSaveTextDocumentParams):
    _validate(ls, params.textDocument.uri)


_DOCUMENT_SYMBOL_KINDS = {
    'module': types.SymbolKind.Module,
    'class': types.SymbolKind.Class,
    'function': types.SymbolKind.Function,
    'statement': types.SymbolKind.Variable,
    'instance': types.SymbolKind.Variable,
    '_pseudotreenameclass': types.SymbolKind.Class
}


def _get_document_symbols(
        code_lines: List[str],
        names: List[Name],
        current: Optional[Name] = None
) -> List[types.DocumentSymbol]:
    # Looks like names are sorted by order of appearance, so
    # children are after their parents
    result = []
    while names:
        if current and names[0].parent() != current:
            break
        name = names.pop(0)
        if name.type == 'param':
            continue
        children = _get_document_symbols(
            code_lines,
            names,
            name
        )
        line = name.line - 1
        r = types.Range(
            types.Position(line, name.column),
            types.Position(line, len(code_lines[line]) - 1)
        )
        result.append(types.DocumentSymbol(
            name.name,
            _DOCUMENT_SYMBOL_KINDS.get(name.type, types.SymbolKind.Null),
            r,
            r,
            children=children or None
        ))
    return result


def _document_symbol_hierarchy(
        uri: str, code_lines: List[str], names: List[Name]
) -> List[types.DocumentSymbol]:
    return _get_document_symbols(code_lines, names)


def _document_symbol_plain(
        uri: str, code_lines: List[str], names: List[Name]
) -> List[types.SymbolInformation]:
    def _symbols():
        for name in names:
            if name.type == 'param':
                continue
            parent = name.parent()
            parent_name = parent and parent.full_name
            if parent_name:
                module_name = name.module_name
                if parent_name == module_name:
                    parent_name = None
                elif parent_name.startswith(f'{module_name}.'):
                    parent_name = parent_name[len(module_name) + 1:]
            yield types.SymbolInformation(
                name.name,
                _DOCUMENT_SYMBOL_KINDS.get(name.type, types.SymbolKind.Null),
                types.Location(uri, types.Range(
                    types.Position(name.line - 1, name.column),
                    types.Position(name.line - 1,
                                   len(code_lines[name.line - 1]) - 1)
                )),
                parent_name
            )
    return list(_symbols())


@server.feature(DOCUMENT_SYMBOL)
def document_symbol(
        ls: LanguageServer, params: types.DocumentSymbolParams
) -> Union[List[types.DocumentSymbol], List[types.SymbolInformation], None]:
    script = get_script(ls, params.textDocument.uri)
    names = script.get_names(all_scopes=True)
    if not names:
        return None
    result = documentSymbolFunction(
        params.textDocument.uri,
        script._code_lines,
        script.get_names(all_scopes=True)
    )
    return result


def _get_text_edits(changes: ChangedFile) -> List[types.TextEdit]:
    result = []
    old_lines = split_lines(changes._module_node.get_code(), keepends=True)
    new_lines = split_lines(changes.get_new_code(), keepends=True)
    line_number = 0
    start = None
    replace_lines = False
    lines: List[str] = []

    def _append():
        if replace_lines:
            end = types.Position(line_number)
        else:
            end = start
        result.append(
            types.TextEdit(
                types.Range(start, end),
                ''.join(lines)
            )
        )

    for line in differ.compare(old_lines, new_lines):
        kind = line[0]
        if kind == '?':
            continue
        if kind == '-':
            if not start:
                start = types.Position(line_number)
            replace_lines = True
            line_number += 1
            continue
        if kind == '+':
            if not start:
                start = types.Position(line_number)
            lines.append(line[2:])
            continue
        if start:
            _append()
            start = None
            replace_lines = False
            lines = []
        line_number += 1
    if start:
        _append()
    return result


def _get_document_changes(
        ls: LanguageServer, refactoring: Refactoring
) -> List[types.TextDocumentEdit]:
    result = []
    for fn, changes in refactoring.get_changed_files().items():
        text_edits = _get_text_edits(changes)
        if text_edits:
            uri = from_fs_path(fn)
            result.append(types.TextDocumentEdit(
                types.VersionedTextDocumentIdentifier(
                    uri,
                    ls.workspace.get_document(uri).version
                ),
                text_edits
            ))
    return result


@server.feature(CODE_ACTION)
def code_action(
        ls: LanguageServer, params: types.CodeActionParams
) -> Optional[List[types.CodeAction]]:
    if params.range.start != params.range.end:
        # No selection actions
        return None
    script = get_script(ls, params.textDocument.uri)
    try:
        refactoring = script.inline(params.range.start.line + 1,
                                    params.range.start.character)
    except RefactoringError:
        return None
    document_changes = _get_document_changes(ls, refactoring)
    if document_changes:
        return [types.CodeAction(
            'Inline variable',
            types.CodeActionKind.RefactorInline,
            edit=types.WorkspaceEdit(document_changes=document_changes))]
    return None
