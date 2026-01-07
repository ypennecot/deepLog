# DeepLog - Analyse de logs de véhicules marins

Application web pour l'analyse et la visualisation de données de logs provenant de véhicules marins autonomes (USV et AUV).

## 🎯 Fonctionnalités principales

- **Import de logs** : Importation automatique des fichiers CSV de navigation et de configuration
- **Visualisation interactive** : Graphiques temporels synchronisés pour analyser le comportement des véhicules
- **Gestion multi-véhicules** : Support simultané pour USV (Unmanned Surface Vehicle) et AUV (Autonomous Underwater Vehicle)
- **Positionnement acoustique** : Décodage et visualisation des données USBL (Ultra-Short Baseline)
- **Graphiques avancés** :
  - Profondeur et altitude
  - Batterie (courant, niveau, tension)
  - Moteurs (8 moteurs)
  - État du véhicule
  - Position GPS (USV)
  - Bearing et distance acoustique
  - Température de l'eau
  - Orientation (pitch, roll, yaw)
- **Mode plein écran** : Visualisation en plein écran pour chaque graphique
- **Réorganisation des panneaux** : Glisser-déposer pour réorganiser les graphiques
- **Zoom et navigation temporelle** : Zoom sur des périodes spécifiques avec curseur synchronisé
- **Vidéos synchronisées** : Affichage de vidéos associées aux runs avec synchronisation temporelle

## 📋 Prérequis

- **Python** 3.8 ou supérieur
- **PostgreSQL** 15+ (avec extension TimescaleDB optionnelle)
- **pip** (gestionnaire de paquets Python)
- **ffmpeg** (pour le traitement vidéo, optionnel)

## 🚀 Installation rapide

Voir le [Guide d'installation détaillé](INSTALLATION.md) pour les instructions complètes.

### Installation minimale

1. **Cloner le dépôt** :
```bash
git clone <repository-url>
cd deepLog
```

2. **Installer les dépendances Python** :
```bash
pip install -r requirements.txt
```

3. **Configurer la base de données** :
   - Créer un fichier `.env` avec vos paramètres de connexion PostgreSQL
   - Voir la section Configuration ci-dessous

4. **Initialiser la base de données** :
```bash
python database/setup.py
```

5. **Lancer l'application** :
```bash
python app.py
```

L'application sera accessible à l'adresse : **http://localhost:5000**

## ⚙️ Configuration

### Variables d'environnement

Créez un fichier `.env` à la racine du projet avec les paramètres suivants :

```env
# Base de données PostgreSQL
DB_HOST=localhost
DB_PORT=5432
DB_NAME=deeplog
DB_USER=your_username
DB_PASSWORD=your_password

# Clé secrète Flask (optionnel, générée automatiquement si absente)
SECRET_KEY=your-secret-key-here
```

**Note importante** : 
- Sur **macOS** avec Homebrew PostgreSQL, si `DB_USER` n'est pas défini ou est défini à `postgres`, l'application utilisera automatiquement votre nom d'utilisateur système
- Sur **Windows/Linux**, l'utilisateur par défaut est `postgres` si non spécifié

### Configuration de la base de données

L'application détecte automatiquement la plateforme et configure la connexion en conséquence :
- **macOS** : Utilise le nom d'utilisateur système par défaut
- **Windows/Linux** : Utilise `postgres` par défaut

## 📖 Utilisation

### 1. Importer des logs

```bash
python database/import_logs.py <chemin_vers_logs>
```

Exemple :
```bash
python database/import_logs.py logs/20251123-monaco/raw_data/logs
```

Le script importe automatiquement :
- Les fichiers `*navigation*.csv` (USV et AUV)
- Les fichiers `*settings*.csv` (uniquement pour AUV)
- Les fichiers USBL (si disponibles)

### 2. Accéder à l'interface web

1. Lancer l'application : `python app.py`
2. Ouvrir un navigateur : http://localhost:5000
3. Naviguer dans les onglets :
   - **Runs AUV** : Liste des runs AUV avec graphiques détaillés
   - **Runs USV** : Liste des runs USV
   - **USBL** : Décodage et visualisation des données USBL
   - **Vidéos** : Gestion des vidéos associées aux runs

### 3. Visualiser un run détaillé

1. Cliquer sur un run dans la liste
2. La page de détail affiche :
   - Graphiques temporels synchronisés
   - Carte de position (si disponible)
   - Informations sur le run (période, durée, etc.)
3. **Interactions disponibles** :
   - **Zoom** : Clic droit pour zoomer sur une période
   - **Curseur** : Déplacement de la souris pour voir les valeurs à un instant donné
   - **Plein écran** : Bouton ⛶ pour afficher un graphique en plein écran
   - **Réorganisation** : Glisser-déposer les panneaux pour les réorganiser
   - **Visibilité** : Cases à cocher pour afficher/masquer des courbes

## 🏗️ Structure du projet

```
deepLog/
├── app.py                      # Application Flask principale
├── database/
│   ├── setup.py               # Script de création de la base de données
│   ├── import_logs.py         # Script d'import des logs
│   ├── decode_usbl.py         # Décodage des messages USBL
│   └── verify_import_precision.py  # Vérification de précision
├── templates/
│   ├── index.html             # Page principale
│   ├── run_detail.html        # Page de détail d'un run
│   └── settings.html          # Page de configuration
├── static/                     # Fichiers statiques (CSS, JS, vidéos)
├── logs/                      # Répertoire des logs (non versionné)
├── specs/                     # Spécifications et documentation
├── antenna_driver_analysis/   # Analyse du driver d'antenne
├── requirements.txt           # Dépendances Python
├── .env                       # Configuration (non versionné)
└── README.md                  # Ce fichier
```

## 🗄️ Architecture de la base de données

L'architecture est optimisée pour les performances avec TimescaleDB :

- **Normalisation** : Types de données stockés comme IDs (références)
- **Partitionnement temporel** : Partitionnement automatique par TimescaleDB
- **Compression** : Compression automatique des données anciennes (>7 jours)
- **Index optimisés** : Index pour les requêtes temporelles

Voir `specs/001-log-analysis/data-model.md` pour plus de détails.

## 🔧 Développement

### Structure des données

Les logs CSV doivent avoir le format suivant :

**Navigation logs** (3 colonnes) :
```
timestamp,data_type,value
2025-01-27 10:00:00,Depth,5.2
2025-01-27 10:00:01,Altitude Kogger,3.1
```

**Settings logs** (2 colonnes) :
```
setting_name,setting_value
MAX_DEPTH,50
TARGET_ALTITUDE,5
```

### Types de données supportés

- **AUV** : Depth, Altitude (Kogger, OA), Batterie, Moteurs, État, BearingPing, etc.
- **USV** : GPS, Position, Orientation, Batterie, Moteurs, État, etc.
- **USBL** : Bearing, Elevation, Distance, SNR

## 🐛 Dépannage

### Erreur de connexion à la base de données

- Vérifier que PostgreSQL est démarré
- Vérifier les paramètres dans `.env`
- Sur macOS, vérifier que le nom d'utilisateur système a les droits PostgreSQL

### Aucune donnée visible

- Vérifier que les logs ont été importés : `python database/verify_import_precision.py`
- Vérifier les filtres dans l'interface web
- Vérifier la console du navigateur pour les erreurs JavaScript

### Erreur "role postgres does not exist" (macOS)

L'application détecte automatiquement macOS et utilise votre nom d'utilisateur système. Si le problème persiste :
- Vérifier que votre utilisateur a les droits PostgreSQL
- Ou définir explicitement `DB_USER` dans `.env`

## 📝 Notes

- Les fichiers settings sont importés uniquement pour les AUV
- Le nom du fichier source est préservé pour chaque entrée de log
- Les graphiques sont synchronisés : le zoom et le curseur sont partagés entre tous les graphiques
- La disposition des panneaux est sauvegardée dans le navigateur (localStorage)

## 📄 Licence

[À compléter selon votre licence]

## 🤝 Contribution

[À compléter selon vos besoins]

## 📞 Support

[À compléter selon vos besoins]
