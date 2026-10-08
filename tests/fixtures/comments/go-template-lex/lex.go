// источник: Go 1.27.1, src/text/template/parse/lex.go, строки 395–440
		if l.parenDepth == 0 {
			return lexRightDelim
		}
		return l.errorf("unclosed left paren")
	}
	switch r := l.next(); {
	case r == eof:
		return l.errorf("unclosed action")
	case isSpace(r):
		l.backup() // Put space back in case we have " -}}".
		return lexSpace
	case r == '=':
		return l.emit(itemAssign)
	case r == ':':
		if l.next() != '=' {
			return l.errorf("expected :=")
		}
		return l.emit(itemDeclare)
	case r == '|':
		return l.emit(itemPipe)
	case r == '"':
		return lexQuote
	case r == '`':
		return lexRawQuote
	case r == '$':
		return lexVariable
	case r == '\'':
		return lexChar
	case r == '.':
		// special look-ahead for ".field" so we don't break l.backup().
		if l.pos < Pos(len(l.input)) {
			r := l.input[l.pos]
			if r < '0' || '9' < r {
				return lexField
			}
		}
		fallthrough // '.' can start a number.
	case r == '+' || r == '-' || ('0' <= r && r <= '9'):
		l.backup()
		return lexNumber
	case isAlphaNumeric(r):
		l.backup()
		return lexIdentifier
	case r == '(':
		l.parenDepth++
		return l.emit(itemLeftParen)
