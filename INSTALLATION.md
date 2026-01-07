# Guide d'installation - DeepLog

Ce guide détaille l'installation complète de DeepLog sur différentes plateformes.

## Table des matières

1. [Prérequis](#prérequis)
2. [Installation sur macOS](#installation-sur-macos)
3. [Installation sur Windows](#installation-sur-windows)
4. [Installation sur Linux](#installation-sur-linux)
5. [Configuration de la base de données](#configuration-de-la-base-de-données)
6. [Vérification de l'installation](#vérification-de-linstallation)
7. [Dépannage](#dépannage)

## Prérequis

### Logiciels requis

- **Python** 3.8 ou supérieur
- **PostgreSQL** 15 ou supérieur
- **pip** (gestionnaire de paquets Python)
- **Git** (pour cloner le dépôt)

### Optionnel

- **TimescaleDB** (extension PostgreSQL pour les séries temporelles)
- **ffmpeg** (pour le traitement vidéo)

## Installation sur macOS

### 1. Installer Python

Vérifier si Python est installé :
```bash
python3 --version
```

Si Python n'est pas installé, l'installer via Homebrew :
```bash
brew install python3
```

### 2. Installer PostgreSQL

```bash
brew install postgresql@15
brew services start postgresql@15
```

### 3. Installer TimescaleDB (optionnel mais recommandé)

```bash
brew install timescaledb
timescaledb-tune --quiet --yes
```

Puis dans PostgreSQL :
```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

### 4. Cloner le dépôt

```bash
git clone <repository-url>
cd deepLog
```

### 5. Créer un environnement virtuel (recommandé)

```bash
python3 -m venv venv
source venv/bin/activate
```

### 6. Installer les dépendances Python

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 7. Configurer la base de données

Créer un fichier `.env` à la racine du projet :

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=deeplog
DB_USER=
DB_PASSWORD=
```

**Note** : Sur macOS, si `DB_USER` est vide ou défini à `postgres`, l'application utilisera automatiquement votre nom d'utilisateur système (celui retourné par `whoami`).

### 8. Initialiser la base de données

```bash
python database/setup.py
```

Cette commande :
- Crée la base de données `deeplog` si elle n'existe pas
- Crée toutes les tables nécessaires
- Configure les index et les politiques de compression (si TimescaleDB est disponible)

### 9. Lancer l'application

```bash
python app.py
```

L'application sera accessible à : **http://localhost:5000**

## Installation sur Windows

### 1. Installer Python

1. Télécharger Python depuis [python.org](https://www.python.org/downloads/)
2. Installer en cochant "Add Python to PATH"
3. Vérifier l'installation :
```cmd
python --version
```

### 2. Installer PostgreSQL

1. Télécharger PostgreSQL depuis [postgresql.org](https://www.postgresql.org/download/windows/)
2. Installer avec les options par défaut
3. Noter le mot de passe du superutilisateur `postgres`

### 3. Installer TimescaleDB (optionnel)

1. Télécharger depuis [timescale.com](https://docs.timescale.com/install/latest/self-hosted/windows/)
2. Suivre les instructions d'installation
3. Dans PostgreSQL, créer l'extension :
```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

### 4. Cloner le dépôt

```cmd
git clone <repository-url>
cd deepLog
```

### 5. Créer un environnement virtuel

```cmd
python -m venv venv
venv\Scripts\activate
```

### 6. Installer les dépendances

```cmd
pip install --upgrade pip
pip install -r requirements.txt
```

### 7. Configurer la base de données

Créer un fichier `.env` :

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=deeplog
DB_USER=postgres
DB_PASSWORD=votre_mot_de_passe_postgres
```

### 8. Initialiser la base de données

```cmd
python database\setup.py
```

### 9. Lancer l'application

```cmd
python app.py
```

## Installation sur Linux

### 1. Installer Python

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3 python3-pip python3-venv

# Fedora/RHEL
sudo dnf install python3 python3-pip
```

### 2. Installer PostgreSQL

```bash
# Ubuntu/Debian
sudo apt install postgresql-15 postgresql-contrib

# Fedora/RHEL
sudo dnf install postgresql15-server postgresql15-contrib
```

Démarrer PostgreSQL :
```bash
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### 3. Installer TimescaleDB (optionnel)

Suivre les instructions sur [docs.timescale.com](https://docs.timescale.com/install/latest/self-hosted/linux/)

### 4. Cloner le dépôt

```bash
git clone <repository-url>
cd deepLog
```

### 5. Créer un environnement virtuel

```bash
python3 -m venv venv
source venv/bin/activate
```

### 6. Installer les dépendances

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 7. Configurer la base de données

Créer un fichier `.env` :

```env
DB_HOST=localhost
DB_PORT=5432
DB_NAME=deeplog
DB_USER=postgres
DB_PASSWORD=votre_mot_de_passe
```

### 8. Initialiser la base de données

```bash
python database/setup.py
```

### 9. Lancer l'application

```bash
python app.py
```

## Configuration de la base de données

### Création manuelle de la base de données

Si vous préférez créer la base de données manuellement :

```sql
-- Se connecter à PostgreSQL
psql -U postgres

-- Créer la base de données
CREATE DATABASE deeplog;

-- Se connecter à la nouvelle base
\c deeplog

-- Créer l'extension TimescaleDB (si installée)
CREATE EXTENSION IF NOT EXISTS timescaledb;
```

Puis exécuter le script de setup :
```bash
python database/setup.py
```

### Configuration des permissions

Assurez-vous que l'utilisateur PostgreSQL a les droits nécessaires :

```sql
-- Donner tous les droits à l'utilisateur
GRANT ALL PRIVILEGES ON DATABASE deeplog TO votre_utilisateur;
```

## Vérification de l'installation

### 1. Vérifier Python

```bash
python3 --version  # Doit afficher 3.8 ou supérieur
```

### 2. Vérifier PostgreSQL

```bash
psql --version  # Doit afficher 15 ou supérieur
```

### 3. Vérifier la connexion à la base de données

```bash
python3 -c "from app import get_db_connection; conn = get_db_connection(); print('Connexion OK'); conn.close()"
```

### 4. Vérifier les dépendances Python

```bash
pip list | grep -E "Flask|psycopg2|pandas"
```

### 5. Tester l'import de logs

```bash
# Importer un petit fichier de test
python database/import_logs.py <chemin_vers_logs>

# Vérifier l'import
python database/verify_import_precision.py
```

### 6. Tester l'application web

1. Lancer l'application : `python app.py`
2. Ouvrir http://localhost:5000
3. Vérifier que la page se charge sans erreur

## Dépannage

### Erreur : "role postgres does not exist" (macOS)

**Solution** : L'application détecte automatiquement macOS et utilise votre nom d'utilisateur système. Si le problème persiste :

1. Vérifier que votre utilisateur a les droits PostgreSQL :
```bash
createuser -s $(whoami)
```

2. Ou définir explicitement `DB_USER` dans `.env` :
```env
DB_USER=votre_nom_utilisateur
```

### Erreur : "ModuleNotFoundError: No module named 'psycopg2'"

**Solution** :
```bash
pip install psycopg2-binary
```

### Erreur : "Could not connect to server"

**Solutions** :
1. Vérifier que PostgreSQL est démarré :
   - macOS : `brew services list`
   - Linux : `sudo systemctl status postgresql`
   - Windows : Vérifier dans les services Windows

2. Vérifier les paramètres dans `.env`

3. Tester la connexion manuellement :
```bash
psql -h localhost -U votre_utilisateur -d deeplog
```

### Erreur : "TimescaleDB extension not available"

**Solution** : TimescaleDB est optionnel. L'application fonctionne sans, mais avec des performances réduites pour les grandes quantités de données.

Pour installer TimescaleDB :
- macOS : `brew install timescaledb`
- Linux : Suivre [docs.timescale.com](https://docs.timescale.com/install/latest/self-hosted/)
- Windows : Télécharger depuis [timescale.com](https://docs.timescale.com/install/latest/self-hosted/windows/)

### L'application ne démarre pas

**Vérifications** :
1. Vérifier que le port 5000 n'est pas déjà utilisé :
```bash
# macOS/Linux
lsof -i :5000

# Windows
netstat -ano | findstr :5000
```

2. Changer le port dans `app.py` si nécessaire :
```python
if __name__ == '__main__':
    app.run(debug=True, port=5001)  # Changer le port
```

### Aucune donnée visible dans l'interface

**Solutions** :
1. Vérifier que les logs ont été importés :
```bash
python database/verify_import_precision.py
```

2. Vérifier les filtres dans l'interface web

3. Vérifier la console du navigateur (F12) pour les erreurs JavaScript

4. Vérifier les logs de l'application Flask dans le terminal

## Prochaines étapes

Une fois l'installation terminée :

1. **Importer vos premiers logs** :
```bash
python database/import_logs.py logs/votre_dossier
```

2. **Explorer l'interface web** : http://localhost:5000

3. **Consulter la documentation** : Voir `README.md` pour les fonctionnalités détaillées

## Support

Pour toute question ou problème :
- Vérifier les logs de l'application
- Consulter la section Dépannage ci-dessus
- [Créer une issue sur GitHub] (si applicable)
