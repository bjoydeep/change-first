IMAGE=quay.io/bjoydeep/pr-analyzer
TAG?=latest
VENV=.venv

venv:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements.txt
	@echo "Run: source $(VENV)/bin/activate"

build:
	podman build --platform linux/amd64 -t $(IMAGE):$(TAG) .

push:
	podman push $(IMAGE):$(TAG)

build-push: build push

run-local:
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8080

run-container:
	podman run --env-file .env -p 8080:8080 $(IMAGE):$(TAG)

test:
	pytest tests/ -v
