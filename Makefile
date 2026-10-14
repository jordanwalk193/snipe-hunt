.PHONY: improve test

# Print the instructions for the monthly improvement pull request.
improve:
	@cat docs/SELF_IMPROVE.md

test:
	python -m pytest -q
