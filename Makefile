##########################################
#### NAS Developer Experience ##
##########################################
##
## If you don't really know what to do, run `make help`.
##

## Image coordinates
IMAGE_NAME ?= nas/bootc
TAG        ?= stable

## ZFS stream to track (prefix of release tag, e.g. zfs-2.4)
ZFS_STREAM ?= zfs-2.4

## Tool variables (override on the command line when needed)
DOCKER       ?= docker
PODMAN       ?= podman
GH           ?= gh
SKOPEO       ?= skopeo
JQ           ?= jq
QEMU         ?= qemu-system-x86_64
QEMU_IMG     ?= qemu-img
TIMEOUT      ?= timeout
OVMF_CODE    ?= /usr/share/edk2/ovmf/OVMF_CODE.fd
CODEX        ?= codex
MCP_GRAFANA_URL ?= https://visualize.i.samhclark.com
MCP_GRAFANA_TOKEN_REF ?= op://Private/Grafana Service Account/credential
BUTANE_IMAGE ?= quay.io/coreos/butane:release@sha256:13fec166cb47a8e053dcc23256c0ca41aaa1c61cab39793832aaf8894ca78c8f
SHELLCHECK_IMAGE ?= docker.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d
UV           ?= uv
UV_RUN       := $(UV) run --locked

SHELL_SOURCES := $(shell \
	git ls-files '*.sh' ':!:docs/history/**' | \
	while IFS= read -r source; do \
		test ! -f "$$source" || printf '%s\n' "$$source"; \
	done)

## Colors
COLOR_BLUE  = \033[34m
COLOR_GREEN = \033[32m
COLOR_RED   = \033[31m
COLOR_RESET = \033[0m

###
### TASKS
###

.DEFAULT_GOAL := all

##@ Default

.PHONY: all
all: deps check test ## Run deps, check, test, and build (default)
	@$(MAKE) --no-print-directory build

##@ Utility

.PHONY: help
help: ## Display this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

.PHONY: mcp
mcp: ## Start Codex with the Grafana MCP token loaded from 1Password
	@set -euo pipefail; \
	command -v op >/dev/null || { printf "$(COLOR_RED)op is required$(COLOR_RESET)\n" >&2; exit 1; }; \
	command -v "$(CODEX)" >/dev/null || { printf "$(COLOR_RED)$(CODEX) is required$(COLOR_RESET)\n" >&2; exit 1; }; \
	token="$$(op read "$(MCP_GRAFANA_TOKEN_REF)")"; \
	test -n "$$token" || { printf "$(COLOR_RED)1Password returned an empty Grafana token$(COLOR_RESET)\n" >&2; exit 1; }; \
	printf "$(COLOR_GREEN)Starting Codex with Grafana MCP credentials$(COLOR_RESET)\n"; \
	GRAFANA_URL="$(MCP_GRAFANA_URL)" \
	GRAFANA_SERVICE_ACCOUNT_TOKEN="$$token" \
		exec "$(CODEX)" -c 'mcp_servers.grafana.env_vars=["GRAFANA_URL","GRAFANA_SERVICE_ACCOUNT_TOKEN"]'

##@ Information

.PHONY: zfs-version
zfs-version: ## Get the latest ZFS version (e.g. 2.4.2)
	@./scripts/resolve-zfs-version.sh $(ZFS_STREAM)

.PHONY: kernel-version
kernel-version: ## Get the current kernel version from Fedora CoreOS stable
	@./scripts/query-coreos-kernel.sh

.PHONY: versions
versions: ## Show all relevant versions and verify ZFS kmod availability
	@set -e; \
	BUILD_INPUTS="$$(GH_BIN="$(GH)" JQ_BIN="$(JQ)" SKOPEO_BIN="$(SKOPEO)" \
		CONTAINER_CLI="$(DOCKER)" \
		./scripts/resolve-build-inputs.sh "$(ZFS_STREAM)")"; \
	set -- $$BUILD_INPUTS; \
	ZFS_VERSION="$$1"; \
	KERNEL_VERSION="$$2"; \
	IMAGE="$$3"; \
	printf "ZFS Version:    %s\n" "$$ZFS_VERSION"; \
	printf "Kernel Version: %s\n" "$$KERNEL_VERSION"; \
	printf "Kmod Image:     %s\n" "$$IMAGE"; \
	printf "$(COLOR_GREEN)ZFS kmods available$(COLOR_RESET)\n"

##@ Development

.PHONY: check
check: typecheck check-generated check-shell check-ignition check-vm-ignition ## Run static, non-mutating repository checks

.PHONY: check-generated
check-generated: ## Verify generated artifacts are current without changing them
	@$(UV_RUN) python generate-quadlets.py --check

.PHONY: check-shell
check-shell: ## Check all maintained shell programs
	@$(PODMAN) run --rm \
		--security-opt label=disable \
		--volume "$(PWD)":/mnt:ro --workdir /mnt \
		$(SHELLCHECK_IMAGE) --severity=warning $(SHELL_SOURCES)

.PHONY: check-ignition
check-ignition: ## Validate Butane strictly without writing ignition.json
	@$(PODMAN) run --rm --interactive --network=none \
		--read-only --cap-drop=all --security-opt=no-new-privileges \
		$(BUTANE_IMAGE) --strict < butane.yaml >/dev/null

.PHONY: check-vm-ignition
check-vm-ignition: ## Validate the storage-free VM smoke Ignition fixture
	@$(PODMAN) run --rm --interactive --network=none \
		--read-only --cap-drop=all --security-opt=no-new-privileges \
		$(BUTANE_IMAGE) --strict < tests/vm-smoke.bu >/dev/null

.PHONY: check-zfs-available
check-zfs-available: ## Verify prebuilt ZFS kmods exist for the current versions
	@GH_BIN="$(GH)" JQ_BIN="$(JQ)" SKOPEO_BIN="$(SKOPEO)" \
		CONTAINER_CLI="$(DOCKER)" \
		./scripts/resolve-build-inputs.sh "$(ZFS_STREAM)" >/dev/null
	@printf "$(COLOR_GREEN)ZFS kmods available$(COLOR_RESET)\n"

.PHONY: test
test: ## Run unit tests
	@$(UV_RUN) python -m unittest discover -s tests -v

.PHONY: smoke-immich-images
smoke-immich-images: ## Run Podman-only smoke tests for pinned Immich companion images
	@CONTAINER_CLI="$(PODMAN)" $(UV_RUN) python scripts/smoke-immich-images.py

ARR_SMOKE_STARTUP_TIMEOUT_SECONDS ?= 60
ARR_SMOKE_OBSERVE_SECONDS ?= 10
.PHONY: smoke-arr-images
smoke-arr-images: ## Run opt-in startup smoke tests for the four authored *arr images
	@CONTAINER_CLI="$(PODMAN)" \
		ARR_SMOKE_STARTUP_TIMEOUT_SECONDS="$(ARR_SMOKE_STARTUP_TIMEOUT_SECONDS)" \
		ARR_SMOKE_OBSERVE_SECONDS="$(ARR_SMOKE_OBSERVE_SECONDS)" \
		$(UV_RUN) python scripts/smoke-arr-images.py

.PHONY: probe-krun-user
probe-krun-user: ## Probe the pinned Immich database image's effective krun user
	@CONTAINER_CLI="$(PODMAN)" $(UV_RUN) python scripts/probe-krun-user.py

.PHONY: preflight-immich-images
preflight-immich-images: ## Run the complete opt-in Immich image preflight
	@$(MAKE) --no-print-directory smoke-immich-images
	@$(MAKE) --no-print-directory probe-krun-user

.PHONY: typecheck
typecheck: ## Run strict static Python type checks
	@$(UV_RUN) ty check

##@ Building

.PHONY: build
build: ## Build the container image
	@set -e; \
	BUILD_INPUTS="$$(GH_BIN="$(GH)" JQ_BIN="$(JQ)" SKOPEO_BIN="$(SKOPEO)" \
		CONTAINER_CLI="$(DOCKER)" \
		./scripts/resolve-build-inputs.sh "$(ZFS_STREAM)")"; \
	set -- $$BUILD_INPUTS; \
	ZFS_VERSION="$$1"; \
	KERNEL_VERSION="$$2"; \
	printf "$(COLOR_BLUE)Building $(IMAGE_NAME):$(TAG) with ZFS=$$ZFS_VERSION kernel=$$KERNEL_VERSION$(COLOR_RESET)\n"; \
	$(DOCKER) buildx build --file Containerfile --load --pull \
		--build-arg ZFS_VERSION="$$ZFS_VERSION" \
		--build-arg KERNEL_VERSION="$$KERNEL_VERSION" \
		-t "$(IMAGE_NAME):$(TAG)" \
		.; \
	CONTAINER_CLI="$(DOCKER)" \
		./scripts/verify-built-image.sh "$(IMAGE_NAME):$(TAG)"; \
	printf "$(COLOR_GREEN)build succeeded: $(IMAGE_NAME):$(TAG)$(COLOR_RESET)\n"

.PHONY: verify-image
verify-image: ## Verify the exact locally built image without network or host mounts
	@CONTAINER_CLI="$(DOCKER)" \
		./scripts/verify-built-image.sh "$(IMAGE_NAME):$(TAG)"

.PHONY: test-vm
test-vm: deps-vm ## Boot a fresh QCOW in an isolated VM; set QCOW=/absolute/path
	@test -n "$(QCOW)" || \
		(printf "$(COLOR_RED)Set QCOW to an absolute fresh QCOW2 image path.$(COLOR_RESET)\n" && false)
	@CONTAINER_CLI="$(PODMAN)" BUTANE_IMAGE="$(BUTANE_IMAGE)" \
		QEMU_BIN="$(QEMU)" QEMU_IMG_BIN="$(QEMU_IMG)" JQ_BIN="$(JQ)" \
		TIMEOUT_BIN="$(TIMEOUT)" OVMF_CODE="$(OVMF_CODE)" \
		./scripts/run-vm-smoke.sh "$(QCOW)"

.PHONY: generate-ignition
generate-ignition: ## Generate ignition.json from butane.yaml
	@printf "$(COLOR_BLUE)Generating ignition.json from butane.yaml...$(COLOR_RESET)\n"
	@$(PODMAN) run --rm --interactive --network=none \
		--read-only --cap-drop=all --security-opt=no-new-privileges \
		$(BUTANE_IMAGE) --strict < butane.yaml > ignition.json
	@printf "$(COLOR_GREEN)Generated ignition.json$(COLOR_RESET)\n"

.PHONY: generate-quadlets
generate-quadlets: ## Generate quadlet files using the custom generator
	@printf "$(COLOR_BLUE)Generating quadlet files from config...$(COLOR_RESET)\n"
	@$(UV_RUN) python generate-quadlets.py
	@printf "$(COLOR_GREEN)Generated quadlet files$(COLOR_RESET)\n"

##@ GitHub Workflows

.PHONY: publish
publish: ## Trigger the production image publishing workflow
	@$(GH) workflow run build.yaml
	@printf "$(COLOR_GREEN)Triggered build.yaml$(COLOR_RESET)\n"

.PHONY: run-pages
run-pages: ## Trigger Ignition file generation and GitHub Pages deployment
	@$(GH) workflow run pages.yaml
	@printf "$(COLOR_GREEN)Triggered pages.yaml$(COLOR_RESET)\n"

.PHONY: run-cleanup
run-cleanup: ## Trigger container image cleanup workflow (dry run by default)
	@$(GH) workflow run cleanup-images.yaml
	@printf "$(COLOR_GREEN)Triggered cleanup-images.yaml (dry run)$(COLOR_RESET)\n"

.PHONY: run-cleanup-force
run-cleanup-force: ## Trigger container image cleanup workflow (deletes images)
	@$(GH) workflow run cleanup-images.yaml -f dry_run=false
	@printf "$(COLOR_GREEN)Triggered cleanup-images.yaml (force)$(COLOR_RESET)\n"

.PHONY: workflow-status
workflow-status: ## Show recent build workflow runs
	@$(GH) run list --workflow=build.yaml --limit=5

.PHONY: all-workflows
all-workflows: ## Show recent runs for all workflows
	@printf "$(COLOR_BLUE)Build:$(COLOR_RESET)\n"
	@$(GH) run list --workflow=build.yaml --limit=3
	@echo ""
	@printf "$(COLOR_BLUE)Build Check:$(COLOR_RESET)\n"
	@$(GH) run list --workflow=build-check.yaml --limit=3
	@echo ""
	@printf "$(COLOR_BLUE)Cleanup:$(COLOR_RESET)\n"
	@$(GH) run list --workflow=cleanup-images.yaml --limit=3
	@echo ""
	@printf "$(COLOR_BLUE)Pages:$(COLOR_RESET)\n"
	@$(GH) run list --workflow=pages.yaml --limit=3

RETENTION_DAYS ?= 90
.PHONY: cleanup-dry-run
cleanup-dry-run: ## Plan cleanup locally; set RETENTION_DAYS=N to configure (default: 90)
	@$(UV_RUN) ./scripts/select-expired-images.sh $(RETENTION_DAYS)

##@ Dependencies

.PHONY: deps
deps: deps-check-docker deps-check-podman deps-check-gh deps-check-skopeo deps-check-jq deps-check-uv ## Check tools and sync the Python environment
	@printf "$(COLOR_GREEN)All deps present!$(COLOR_RESET)\n"

.PHONY: deps-vm
deps-vm: deps-check-podman deps-check-jq ## Check optional VM smoke dependencies without installing them
	@$(PODMAN) image exists "$(BUTANE_IMAGE)" || \
		(printf "$(COLOR_RED)Pinned Butane image is not local; run make check first.$(COLOR_RESET)\n" && false)
	@command -v $(QEMU) >/dev/null || \
		(printf "$(COLOR_RED)$(QEMU) not found.$(COLOR_RESET)\n" && false)
	@command -v $(QEMU_IMG) >/dev/null || \
		(printf "$(COLOR_RED)$(QEMU_IMG) not found.$(COLOR_RESET)\n" && false)
	@command -v $(TIMEOUT) >/dev/null || \
		(printf "$(COLOR_RED)$(TIMEOUT) not found.$(COLOR_RESET)\n" && false)
	@test -r "$(OVMF_CODE)" || \
		(printf "$(COLOR_RED)OVMF firmware not readable: $(OVMF_CODE)$(COLOR_RESET)\n" && false)
	@test -r /dev/kvm && test -w /dev/kvm || \
		(printf "$(COLOR_RED)/dev/kvm is not accessible.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_GREEN)VM smoke deps present!$(COLOR_RESET)\n"

.PHONY: deps-check-podman
deps-check-podman: ## Check that podman is available
	@command -v $(PODMAN) > /dev/null || \
		(printf "$(COLOR_RED)$(PODMAN) not found. Install via your system package manager.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_BLUE)podman: $$($(PODMAN) --version)$(COLOR_RESET)\n"

.PHONY: deps-check-docker
deps-check-docker: ## Check that Docker Engine and Buildx are available
	@command -v $(DOCKER) > /dev/null || \
		(printf "$(COLOR_RED)$(DOCKER) not found. Install Docker Engine with Buildx.$(COLOR_RESET)\n" && false)
	@$(DOCKER) info > /dev/null || \
		(printf "$(COLOR_RED)Docker Engine is not available.$(COLOR_RESET)\n" && false)
	@$(DOCKER) buildx version > /dev/null || \
		(printf "$(COLOR_RED)Docker Buildx is not available.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_BLUE)docker: $$($(DOCKER) --version)$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)buildx: $$($(DOCKER) buildx version)$(COLOR_RESET)\n"

.PHONY: deps-check-gh
deps-check-gh: ## Check that the GitHub CLI is available
	@command -v $(GH) > /dev/null || \
		(printf "$(COLOR_RED)gh not found. See https://cli.github.com for install instructions.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_BLUE)gh: $$($(GH) --version | head -1)$(COLOR_RESET)\n"

.PHONY: deps-check-skopeo
deps-check-skopeo: ## Check that skopeo is available
	@command -v $(SKOPEO) > /dev/null || \
		(printf "$(COLOR_RED)skopeo not found. Install via your system package manager.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_BLUE)skopeo: $$($(SKOPEO) --version)$(COLOR_RESET)\n"

.PHONY: deps-check-jq
deps-check-jq: ## Check that jq is available
	@command -v $(JQ) > /dev/null || \
		(printf "$(COLOR_RED)$(JQ) not found. Install via your system package manager.$(COLOR_RESET)\n" && false)
	@printf "$(COLOR_BLUE)jq: $$($(JQ) --version)$(COLOR_RESET)\n"

.PHONY: deps-check-uv
deps-check-uv: ## Check uv and synchronize the locked Python environment
	@command -v $(UV) > /dev/null || \
		(printf "$(COLOR_RED)uv not found. Install it from https://docs.astral.sh/uv/.$(COLOR_RESET)\n" && false)
	@$(UV) sync --locked
	@printf "$(COLOR_BLUE)uv: $$($(UV) --version)$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)python: $$($(UV_RUN) python --version)$(COLOR_RESET)\n"
	@printf "$(COLOR_BLUE)ty: $$($(UV_RUN) ty --version)$(COLOR_RESET)\n"
