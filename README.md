# etoro-agent-workflow

Socle technique du projet **Bot IA eToro** : un agent IA (workflow Mistral) pilote un
**portefeuille agent eToro** via l'API publique pendant des sessions de trading automatisé.
Exécution 100 % Mistral Workflows. La stratégie de trading n'est PAS encore branchée :
le socle expose une interface (`worker/strategy.py`) où elle se branchera plus tard.

## Architecture

- **3 workflows** découverts automatiquement par le worker :
  - `session-poll` : un tour de poll de session (lecture compte + marché, gestion des stops,
    évaluation d'une entrée sous garde-fous). Stateless : chaque tour relit tout depuis l'API.
  - `ntfy-notify` : publication d'une notification.
  - `smoke` : health check sans appel externe.
- **Sessions** : une session = une programmation bornée créée côté Vibe (connecteur
  `mistral_mcp`, `create_workflow_schedule`) : intervalle `PT5M` + fin bornée.
- **Garde-fous** (codés dans `worker/rules.py`, hors de portée de toute stratégie) :
  fenêtre 14h30-16h30 Paris, dernière entrée 16h05, clôture forcée 16h15, 2 positions max,
  15 trades/jour, stop journalier -1 %, SL soft 6 pips, filet broker SL 1 %, risque 0,5 %/trade,
  pause après 3 pertes, spread max, exposition minimale.dry_run (`config.dry_run`) ne passe AUCUN ordre.
- **Failsafes** : User-Agent custom (WAF eToro bannit l'UA Python), `x-request-id` idempotent
  (retry interne unique par ordre, Temporal ne rejoue jamais un ordre), halt de session sur
  échec d'ordre, activités single-attempt pour tout ce qui change l'état du compte.

## Configuration

Secrets workspace Mistral (jamais dans le repo) :
- `ETORO_TOKEN` : le token utilisateur du portefeuille agent (visible UNE fois à sa création).
  Alternative : `ETORO_USER_KEY` + `ETORO_API_KEY` (auth paire de clés, compte propre).
- `ETORO_ACCOUNT` : `real` (agent portfolio) ou `demo` (routes miroir démo).
- `NTFY_TOPIC` : topic de notification.

## Routes API

Base `https://public-api.etoro.com/api/v1` (carte dans `worker/etoro.py`). Les routes démo
sont éprouvées en production (21/11/2026) ; les routes réelles (lecture + exécution) sont
**à revalider en dry-run contre le catalogue vivant** avant le premier ordre réel.

## CI

- `generate-uv-lock` : régénère et commite `uv.lock` à chaque push de `pyproject.toml`.
  Ne JAMAIS déployer avant que le lock soit committé (`uv sync --frozen` le refuse).
- `socle checks` : compile tous les modules + 10 contrôles purs des garde-fous
  (`apps/worker/tests/test_rules.py`).

## Déploiement

Console Mistral → Build/Workflows : déploiement géré construit depuis ce repo (branche `main`,
build dir `.`, Dockerfile `deploy/docker/Dockerfile.worker`). Attendre `wait ready` sur la
révision servie avant tout test.

## Avertissement

Le trading automatisé expose le montant investi au risque de marché complet (perte en capital
possible). Les garde-fous réduisent certains risques mais n'éliminent aucun risque de marché.
