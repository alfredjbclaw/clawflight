.PHONY: test gate pii history demo clean

# The whole gate: offline, no credentials, no network.
test:
	python3 -m pytest tests -q

# Everything that must be green before publishing.
gate: test history
	@echo "gate: clean"

pii:
	./tests/pii_scan.py --worktree
	./tests/pii_scan.py

history:
	./tests/pii_scan.py

# Drive the CLI end to end against the synthetic fixtures in a scratch dir.
demo:
	@tmp=$$(mktemp -d) && \
	python3 -m clawflight.cli --config examples/clawflight.demo.json \
	  --state-dir $$tmp --json sweep --dry-run \
	  --mailbox mbox:fixtures/inbox.mbox && \
	python3 -m clawflight.cli --config examples/clawflight.demo.json \
	  --state-dir $$tmp status && \
	python3 -m clawflight.cli --config examples/clawflight.demo.json \
	  --state-dir $$tmp doctor; \
	rm -rf $$tmp

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache *.egg-info build dist
