# источник: Pygments 2.21.0, pygments/lexers/lisp.py, строки 2359–2375 (ShenLexer, сырая f-строка с «\'»)
    MAPPINGS.update((s, Keyword) for s in SPECIAL_FORMS)

    valid_symbol_chars = r'[\w!$%*+,<=>?/.\'@&#:-]'
    valid_name = f'{valid_symbol_chars}+'
    symbol_name = rf'[a-z!$%*+,<=>?/.\'@&#_-]{valid_symbol_chars}*'
    variable = rf'[A-Z]{valid_symbol_chars}*'

    tokens = {
        'string': [
            (r'"', String, '#pop'),
            (r'c#\d{1,3};', String.Escape),
            (r'~[ARS%]', String.Interpol),
            (r'(?s).', String),
        ],

        'root': [
            (r'(?s)\\\*.*?\*\\', Comment.Multiline),  # \* ... *\
