.PHONY: test test-offline gate pii history demo skill-bundle clean

# The whole gate: offline, no credentials, no network.
test:
	python3 -m pytest tests -q

# The same suite with every socket blocked. The engine is meant to run offline;
# this is what proves it rather than asserting it.
test-offline:
	PYTHONPATH=tests python3 -m pytest tests -q -p no:cacheprovider -p no_network_plugin

# Everything that must be green before publishing.
gate: test test-offline history skill-bundle
	@echo "gate: clean"

pii:
	./tests/pii_scan.py --worktree
	./tests/pii_scan.py

history:
	./tests/pii_scan.py

# Assemble the self-contained ClawHub skill and prove it runs with no install.
skill-bundle:
	./scripts/build_skill_bundle.py --verify

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
