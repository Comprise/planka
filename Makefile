.PHONY: test validate install uninstall

test:
	python3 -m unittest discover -s tests -t . -v

validate:
	claude plugin validate .

install:
	mkdir -p "$(HOME)/.claude/skills"
	ln -sfn "$(CURDIR)" "$(HOME)/.claude/skills/planka"
	@echo "planka установлен: $(HOME)/.claude/skills/planka -> $(CURDIR); перезапустите сессию"

uninstall:
	rm -f "$(HOME)/.claude/skills/planka"
