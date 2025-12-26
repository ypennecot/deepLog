# DeepLog - Analyse de logs de véhicules marins

Application pour l'analyse de données de logs provenant de véhicules marins autonomes (USV et AUV).

## Prérequis

- Python 3.8+
- PostgreSQL 15+ avec extension TimescaleDB
- pip (gestionnaire de paquets Python)

## Installation

1. **Cloner le dépôt** (si applicable)

2. **Installer les dépendances Python**:
```bash
pip install -r requirements.txt
```

3. **Configurer la base de données PostgreSQL**:
   - Créer une base de données PostgreSQL
   - Installer l'extension TimescaleDB:
   ```sql
   CREATE EXTENSION IF NOT EXISTS timescaledb;
   ```

4. **Configurer les variables d'environnement**:
   - Copier `.env.example` vers `.env`
   - Modifier les valeurs selon votre configuration:
   ```bash
   cp .env.example .env
   ```

## Configuration

Éditer le fichier `.env` avec vos paramètres de base de données:

```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=deeplog
DB_USER=your_username
DB_PASSWORD=your_password
```

## Utilisation

### 1. Créer la structure de la base de données

```bash
python database/setup.py
```

Cette commande crée:
- La base de données (si elle n'existe pas)
- Toutes les tables selon le schéma optimisé
- Les index et politiques de compression TimescaleDB

### 2. Importer les logs

```bash
python database/import_logs.py logs/20251123-monaco/raw_data/logs
```

Cette commande:
- Importe tous les fichiers `*navigation*.csv` (USV et AUV)
- Importe les fichiers `*settings*.csv` (uniquement pour AUV)
- Préserve le nom du fichier source pour chaque entrée
- Crée automatiquement les types de données dans le catalogue

### 3. Lancer l'interface web

```bash
python app.py
```

L'application sera accessible à l'adresse: http://localhost:5000

## Fonctionnalités de l'interface web

- **Visualisation en tableau**: Affichage paginé des données importées
- **Filtres**:
  - Par type de véhicule (USV/AUV)
  - Par type de donnée
  - Par plage de dates
- **Statistiques**: Vue d'ensemble des données importées
- **Traçabilité**: Affichage du fichier source pour chaque entrée

## Structure du projet

```
deepLog/
├── database/
│   ├── setup.py          # Script de création de la base de données
│   └── import_logs.py    # Script d'import des logs
├── templates/
│   └── index.html        # Interface web
├── app.py                # Application Flask
├── requirements.txt      # Dépendances Python
├── .env.example          # Exemple de configuration
└── README.md             # Ce fichier
```

## Architecture de la base de données

L'architecture est optimisée pour les performances:
- **Normalisation**: Les types de données sont stockés comme IDs (références) au lieu de chaînes
- **TimescaleDB**: Partitionnement temporel automatique pour les séries temporelles
- **Compression**: Compression automatique des données anciennes (>7 jours)
- **Index**: Index optimisés pour les requêtes temporelles

Voir `specs/001-log-analysis/data-model.md` pour plus de détails.

## Notes

- Les fichiers settings sont importés uniquement pour les AUV (selon la spécification)
- Les fichiers USV ignorent les fichiers settings même s'ils sont présents
- Le nom du fichier source est préservé pour chaque entrée de log




