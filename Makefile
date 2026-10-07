.PHONY: test validate

test:
	python3 -m unittest discover -s tests -t . -v

validate:
	claude plugin validate .
	claude plugin validate plugin
