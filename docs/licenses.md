# Third-Party Libraries & Licenses

## Swarm Controller Dependencies

| Library | Version | License | Source | Notes |
|---|---|---|---|---|
| Flask | >=2.0 | BSD-3-Clause | https://github.com/pallets/flask | Web framework, API server |
| requests | >=2.28 | Apache-2.0 | https://github.com/psf/requests | HTTP client for LLM API calls |
| PyYAML | >=6.0 | MIT | https://github.com/yaml/pyyaml | Prompt YAML loading |
| pytest | >=7.0 | MIT | https://github.com/pytest-dev/pytest | Test suite (dev only) |
| playwright | >=1.40 | Apache-2.0 | https://github.com/microsoft/playwright-python | Dashboard browser tests (dev only) |

## Godot Project Dependencies

| Library | Version | License | Source | Notes |
|---|---|---|---|---|
| GUT (Godot Unit Test) | 9.6.0 | MIT | https://github.com/bitwes/Gut | External dependency fetched into local cache; not bundled in this repo |


## LLM APIs (external services, not bundled)

| Service | Terms | Notes |
|---|---|---|
| MiniMax API | https://www.minimaxi.com/en/protocol/terms | Commercial use permitted under API terms |
| Anthropic API | https://www.anthropic.com/legal/aup | Commercial use permitted under API terms |
| OpenRouter | https://openrouter.ai/terms | Passes through to underlying model providers |
