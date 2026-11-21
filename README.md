# Proposition-commerciale

Workflow Mistral (`proposition_commerciale`) qui surveille la feuille Google Sheets
**« Clients fictifs »** et, pour chaque nouveau client saisi (colonne `Prénom` remplie,
colonne `Proposition` vide) :

1. lit la ligne du client (prénom, mail, dernière visite, commande) ;
2. télécharge le modèle `Proposition Commerciale.pdf` depuis Google Drive ;
3. génère le contenu personnalisé de la proposition avec un modèle Mistral ;
4. remplit les champs du modèle PDF (placeholders remplacés) et ajoute une page
   personnalisée, puis dépose le PDF dans le dossier Drive de sortie ;
5. écrit le lien du PDF dans la colonne `Proposition` de la ligne (idempotent : une
   ligne déjà marquée n'est pas retraitée).

## Déclenchement

Les webhooks étant indisponibles sur le plan actuel, le workflow tourne en **polling**
via un schedule (toutes les minutes) sur le déploiement managé. Une exécution manuelle
est aussi possible avec `{"dry_run": false, "client_name": "Jean"}` (filtre sur un
client, `dry_run: true` pour simuler sans écrire).

Pour un déclenchement strictement à la saisie, option : un Google Apps Script
`onEdit` dans la feuille qui appelle l'API d'exécution du workflow
(`POST {WORKFLOWS_BASE_URL}/api/workflows/.../executions` avec la clé API du workspace).

## Configuration (variables d'environnement du déploiement)

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | **Secret workspace** : JSON de la clé du compte de service Google | requis |
| `CLIENTS_SHEET_ID` | ID de la feuille clients | `12UiZsrbL-ooGGgeM88SxaZPxGjPkXBaWeiWl26zPHf0` |
| `CLIENTS_SHEET_TAB` | Onglet | `Feuille 1` |
| `CLIENTS_NAME_COLUMN` | Colonne du nom | `Prénom` |
| `CLIENTS_STATUS_COLUMN` | Colonne de statut (lien PDF) | `Proposition` (colonne E ajoutée si absente) |
| `TEMPLATE_FILE_ID` | ID du modèle PDF | `1-Zeky8c0-sdZJdfa0SnOgaB--R08yWGU` |
| `OUTPUT_FOLDER_ID` | Dossier Drive de sortie | `0AJP4oNWffCarUk9PVA` |
| `PROPOSAL_MODEL` | Modèle Mistral pour le contenu | `mistral-medium-latest` |
| `PLACEHOLDER_MAP_JSON` | Placeholders du PDF → valeurs (`{name}`, `{date}`, `{ref}`, `{mail}`, `{commande}`) | `{"Prénom du client": "{name}", "DATE": "{date}", "CORRES": "{ref}"}` |
| `TEMPLATE_BRIEF` / `OFFER_BRIEF` | consignes envoyées au modèle | description du modèle |

## Mise en route (à faire une fois dans Google Cloud / Studio)

1. Projet Google Cloud : activer **Google Sheets API** et **Google Drive API**.
2. Créer un **compte de service**, télécharger sa clé JSON.
3. Partager avec l'email du compte de service (rôle éditeur) :
   - la feuille « Clients fictifs » ;
   - **le dossier Drive lui-même** `0AJP4oNWffCarUk9PVA` (pas seulement le modèle
     qu'il contient) : c'est lui qui recevra les PDF générés. Sans ce partage du
     dossier, le workflow remonte les PDF dans le Drive du compte de service et les
     partage en lecture avec `DRIVE_SHARE_EMAIL` (défaut `estebancorniere@gmail.com`).
4. Dans Mistral Studio, créer le **secret workspace** `google_service_account_json`
   (<https://console.mistral.ai/secrets>) avec le contenu du fichier JSON de la clé.
5. Lier ce secret au déploiement : page
   <https://console.mistral.ai/workflows/deployments/proposition-commerciale?tab=settings>
   → ajouter un binding : secret workspace `google_service_account_json` → variable
   d'environnement `GOOGLE_SERVICE_ACCOUNT_JSON` (si le nom de variable est laissé
   vide, il vaut `google_service_account_json` — le code accepte les deux). La valeur
   est lue par le worker **au démarrage** : après le binding, redéployer le déploiement.
6. Créer le schedule du workflow `proposition_commerciale` (intervalle `PT1M`).

## Développement local

```bash
cd apps/worker
uv run --all-packages python -m pytest tests/ -q   # tests (effets externes mockés)
bunx nx run worker:semgrep                          # règles workflow (depuis la racine)
```
