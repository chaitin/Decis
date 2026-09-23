# Decis -- build the images and run the services.
#
#   make help
#
# Every target delegates to Docker Compose, so the image tags, build arguments, profiles,
# ports and probes stay defined where they already are: `docker-compose.yml` (what runs, and
# the name it is published under), `docker-compose.override.yml` (how this checkout builds
# it) and `.env` (which engine). Nothing in this file names an engine, an image tag or a
# build argument -- a second copy of any of those is a copy that can drift, and
# `tests/test_makefile.py` fails if one shows up here.
#
# The two ways to run this, and therefore the two kinds of target:
#
#   make up            this checkout builds the images from its own source and runs those
#   make pull          a deployment runs the images the workflow publishes instead
#
# `make images` prints which of the two the current directory would use, so neither of those
# names has to be written down here.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose
comma := ,

# The engine the single-engine targets act on. Asked of Compose rather than read out of
# `.env` here, so that a shell `COMPOSE_PROFILES=... make build-engine` builds the engine
# Compose would use: Compose resolves the shell over `.env`, and a Makefile that read the
# file itself would disagree with the command it wraps. Empty when there is no `.env` yet,
# which is also when there is nothing to build against -- the targets that need an engine say
# so instead of building something arbitrary.
ENGINE ?= $(firstword $(shell $(COMPOSE) config --services 2>/dev/null | grep -v '^playground$$'))
# Every engine with a service, for `build-engines`: the profile names and the engine ids are
# the same list by construction (`tests/test_compose.py` asserts that).
ENGINES := $(shell $(COMPOSE) config --profiles 2>/dev/null)

define need_engine
@test -n "$(ENGINE)" || { echo "error: no engine selected -- set COMPOSE_PROFILES in .env (cp .env.example .env), or pass ENGINE=<id>."; echo "       engines: $(if $(ENGINES),$(ENGINES),<none found: is docker running?>)"; exit 1; }
endef

define need_engines
@test -n "$(ENGINES)" || { echo "error: no engines found -- is docker running, and does .env exist (cp .env.example .env)?"; exit 1; }
endef

# --- building -----------------------------------------------------------------------------

build: ## Build every image the current profile runs (one engine + the playground).
	$(COMPOSE) build

build-engine: ## Build one engine image (ENGINE=<id>). Slow: it bakes that engine's weights.
	$(need_engine)
	$(COMPOSE) build $(ENGINE)

build-engines: ## Build every engine image, whichever profile is active.
	$(need_engines)
	$(COMPOSE) build $(ENGINES)

build-playground: ## Build the playground image. Seconds: its Dockerfile has no RUN.
	$(COMPOSE) build playground

# --- running ------------------------------------------------------------------------------

up: ## Start the engine .env selects and the playground, and wait for the model to answer.
	$(need_engine)
	$(COMPOSE) up -d --wait

up-local: ## Same, but rebuild the images from this checkout first (the full weight bake too).
	$(need_engine)
	$(COMPOSE) up -d --wait --build

up-engine: ## Start only the engine (ENGINE=<id>; default is the one .env selects).
	$(need_engine)
	$(COMPOSE) up -d --wait $(ENGINE)

up-playground: ## Start only the playground: it finds an engine wherever one is running.
	$(COMPOSE) up -d --wait playground

down: ## Stop and remove this project's containers. The images stay.
	$(COMPOSE) down

restart: ## Restart what is running (SERVICE=<name> for one service).
	$(COMPOSE) restart $(SERVICE)

# --- looking at it ------------------------------------------------------------------------

ps: ## What is running: image, ports, health.
	$(COMPOSE) ps

logs: ## Follow the logs (SERVICE=<name> for one service).
	$(COMPOSE) logs -f --tail=100 $(SERVICE)

images: ## The image names this directory builds and runs.
	$(COMPOSE) config --images

config: ## The resolved Compose configuration, every default filled in.
	$(COMPOSE) config

pull: ## Pull the published images (the deployment path) instead of building them.
	$(COMPOSE) -f docker-compose.yml pull

# --- the repository's own checks ----------------------------------------------------------

test: ## The weightless test suite: what CI runs.
	uv run pytest -q

lint: ## ruff check + format check.
	uv run ruff check . && uv run ruff format --check .

# --- the list above -----------------------------------------------------------------------

help: ## List these targets.
	@echo "Decis playground and engines -- images are built from this checkout, services run those images."
	@echo "engine: $(if $(ENGINE),$(ENGINE),<none: cp .env.example .env>)   port: see 'make ps'   override: ENGINE=<id> SERVICE=<name>"
	@echo
	@awk 'BEGIN {FS = ":.*## "}; /^[a-zA-Z][a-zA-Z0-9_-]*:.*## / {printf "  \033[1;36m%-17s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: build build-engine build-engines build-playground up up-local up-engine up-playground \
        down restart ps logs images config pull test lint help
