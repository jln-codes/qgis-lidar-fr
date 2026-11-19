# LIDAR-EXTRACT-01 — Extraction autonome de LiDAR

Recette du 19 novembre 2026, Ubuntu 24.04.5, QGIS 3.44.15-Solothurn.

## Origine et historique Git

Source : `development` de GEMAPI-App, fusion intégrée `a9d07a7` ; contenu LiDAR
issu du commit `40bd605`, conservé sans changement lors de cette fusion.
Le dépôt GitHub `jln-codes/qgis-lidar-fr` existait sans aucune référence Git.

Commande d’extraction :

```sh
git subtree split --prefix=LiDAR development
# ce9f8e04523a537b869feef02b7ef11edbb93d3f
```

Les onze commits pertinents sont conservés avec leurs auteurs, dates et messages.
Leurs identifiants changent car le préfixe disparaît ; l’historique complet
original reste dans GEMAPI-App. Aucun rebase ni push forcé.

| Commit original GEMAPI-App | Commit extrait LiDAR | Message |
| --- | --- | --- |
| 40bd605 | ce9f8e0 | feat(lidar): share library tiles and maintain global coverage VRT |
| c6469aa | 43e2d15 | ahout config LiDAR |
| 3947bcd | 587c2f0 | fix(lidar): compact dialog and close after complete extraction |
| dd6fc94 | f32a179 | fix(lidar): validate IGN Lambert-93 metadata and restore toolbar |
| c05e05f | a524f6f | fix(lidar): move distribution archives to plugin/dist |
| 3cc8da5 | 1042206 | feat(lidar): add standalone LiDAR HD 2026 QGIS plugin |
| 24d45a4 | 36bb5f3 | delete old files |
| 89c24f6 | 3b599a4 | compatibilité script LiDAR avec QGIS 3.40 |
| 89ecc97 | 582ea50 | script async |
| d4c3d31 | 92058d6 | feat(lidar): apply native adaptive Turbo symbology |
| 994f28f | f2c45fb | LiDAR |

## Structure courante

```text
qgis-lidar-fr/
├── lidar_hd_2026/       # package QGIS, modules et icônes actuels
├── tests/              # suite synthétique, benchmark, recette Desktop autonome
├── build_plugin.py
├── lidar_ign.py        # script courant déjà asynchrone, inchangé
├── lidar_ign_async.py  # entrée vers le même script, aucun second moteur
├── test_lidar_ign.py   # contrôle facultatif du script sur un cache existant
├── LiDAR.qgz
├── emprise.gpkg
├── emprise_brute.gpkg
├── README.md
├── LIDAR-EXTRACT-01.md
├── .gitignore
└── dist/               # ZIP local ignoré par Git
```

L’image source de l’icône est également conservée. Le package `lidar_hd_2026`,
le nom **LiDAR HD 2026**, la version 0.1.3 et l’icône courante sont inchangés.
Cette opération change l’organisation et les outils, pas le fonctionnement métier.
Le constructeur écrit uniquement `dist/lidar_hd_2026-0.1.3.zip` ; `--with-sig`
et le constructeur SIG ont disparu. Les chemins des tests et documents partent
de cette racine, sans répertoire `plugin/` extérieur.

Le script alternatif exécute le script asynchrone courant dans le contexte QGIS,
en conservant `iface` et les références des tâches. Le moteur, le script courant,
la configuration IGN, les trois ressources QGIS/GeoPackage et les icônes sont
contrôlés identiques à l’extraction initiale. Aucun paramètre de connexion IGN
modifié. Les scripts console et les données d’exemple restent hors du ZIP.

## Compatibilité avec GEMAPI-App

La seule convention commune est `TOOLBAR_NAME = "SIGMilieuxToolbar"`, titre
**SIG-Milieux**. Chaque extension crée la barre absente, réutilise la barre
existante, ajoute une seule action et retire seulement son action. Le dernier
plugin retire la barre vide. Aucun import entre les deux extensions et aucune
dépendance de compilation, Git ou distribution.

Les tests de chaque dépôt utilisent une action Qt indépendante comme voisin.
La recette des deux ZIP utilise un pilote externe, conservé localement avec
les preuves du chantier ; ce pilote ne fait partie d’aucun plugin.

## Tests et résultats

- Avant extraction : **52 tests** LiDAR réussis dans GEMAPI-App.
- Dépôt autonome : **53 tests** PyQGIS/GDAL réussis, sans réseau IGN, y compris
  moteur, bibliothèque/VRT global, annulation, concurrence, symbologie, barre,
  import installé et entrée asynchrone.
- Script de référence : **4 tests** réussis sur le cache local existant,
  sans téléchargement ; empreintes des GeoTIFF et GeoPackages inchangées.
  Le premier essai du banc supposait encore un `main()` synchrone et laissait
  son journal dans le cache. Le banc a été corrigé pour attendre les callbacks
  Qt et placer tous ses VRT et diagnostics temporaires dans `audits/`.
  Le script de production reste inchangé.
- Benchmark : 100 tuiles synthétiques, trois extractions de deux tuiles,
  sans données utilisateur ni téléchargement. Les sorties restent locales.
- ZIP : manifeste fermé de **11 fichiers**, octets conformes aux sources,
  archives reproductibles, import depuis une extraction isolée.
- Banc des deux ZIP : plugins seuls, deux ordres d’activation, retrait de chacun,
  maintien de l’autre action, puis réactivation ; aucun doublon.
- **QGIS Desktop réel / X11 virtuel** : les mêmes permutations réussissent
  avec chargement/déchargement par le gestionnaire Python QGIS. L’accueil et
  l’assistant intégrés de GEMAPI-App conservent leur comportement.
- **QGIS Desktop réel / Qt offscreen**, LiDAR seul depuis le ZIP : sélection
  de deux polygones par clics Qt, extraction depuis deux tuiles fictives en cache,
  VRT individuel et global conformes, Turbo, fermeture après succès, réouverture
  à progression zéro et projet rouvert après suppression du cache temporaire.
  Le réseau IGN est interdit par le pilote.

Les recettes Desktop sont automatisées, pas des essais humains. Les captures,
JSON et journaux sont locaux sous `audits/` et ne sont pas versionnés.
Windows natif, partage SMB réel, plusieurs écrans physiques et les autres
versions Desktop restent à valider. Aucun nouveau test de connexion IGN réelle.

Commandes depuis ce dépôt :

```sh
python3 build_plugin.py
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s tests -v
QT_QPA_PLATFORM=offscreen /usr/bin/python3 tests/benchmark_library.py
# Facultatif : requiert un cache de tuiles déjà présent, sans téléchargement.
QT_QPA_PLATFORM=offscreen /usr/bin/python3 test_lidar_ign.py
```

La recette Desktop autonome est décrite dans README.md. Pour contrôler les deux
extensions dans QGIS, installer leurs ZIP séparés et vérifier successivement :
GEMAPI-App seul ; LiDAR seul ; GEMAPI-App puis LiDAR ; ordre inverse ; retrait
de GEMAPI-App ; retrait de LiDAR ; réactivation successive des deux. Fermer
les fenêtres fonctionnelles avant de vérifier le retrait final de la barre.

## Nettoyage de GEMAPI-App

Le répertoire source n’est retiré qu’après publication, vérification distante
et reconstruction depuis un clone propre du dépôt LiDAR. Le nettoyage retire
`LiDAR/` et ses références de construction ; le README conserve un lien simple
vers ce dépôt. Le ZIP standard reste construit par son propre outil. Les fichiers
locaux ignorés du répertoire d’origine sont sauvegardés hors dépôt avant retrait.
Le nettoyage est committé sur `development`, intégré dans `main` par fast-forward,
et précède la création de `sirs`. Les audits antérieurs restent dans GEMAPI-App.

## Synchronisation Ubuntu et Windows

Dans le dépôt GEMAPI-App de chaque poste, conserver d’abord les fichiers locaux
et toute modification non committée. Suivre le rapport GEMAPI-TRANSITION-01 pour
le renommage du dossier, le retrait du worktree expérimental et la préparation SIRS.
Puis actualiser sans forcer :

```sh
git fetch origin
git switch development
git merge --ff-only origin/development
# Depuis le dossier parent :
git clone https://github.com/jln-codes/qgis-lidar-fr.git
cd qgis-lidar-fr
python3 build_plugin.py
```

Sous Windows, utiliser PowerShell ou Git Bash pour Git et l’environnement Python
fourni avec QGIS pour les constructions et tests : `python build_plugin.py`, puis
`python -m unittest discover -s tests -v`. Ne pas déplacer un cache LiDAR ou une
bibliothèque permanente lors de la mise à jour : leurs chemins mémorisés ne
changent pas. Les projets utilisant les fichiers de l’ancien dossier `LiDAR/`
doivent pointer vers leurs équivalents conservés ici ; les chemins internes du
projet fourni sont relatifs. Réinstaller les ZIP et redémarrer QGIS pour charger
les nouvelles sources. Ne pas supprimer de travail local en cas de refus du
fast-forward : examiner la divergence avant de poursuivre.

## Publication

La publication normale de `main` est vérifiée sur GitHub au commit technique
`327a4ed2cd05f1381abdf8ecc3f8bcb2a2517fb8`. Un clone neuf du dépôt distant
reconstruit le ZIP et passe le test d’installation isolée ; son archive est
strictement identique, SHA-256 :
`7486e62b4d6e74f4da8d055088d098829338d32f6d1ade27fca17ea7c3518fd2`.

Après ce contrôle, le retrait dans GEMAPI-App est publié par le commit
`04d697ce87d409c2d87acfaec4a6e076022683d1`, sur `development` puis `main`.
Le ZIP standard est reconstruit et testé sans LiDAR ; un clone neuf de GEMAPI-App
reproduit aussi son archive. Le worktree adjacent `gemapi-app-sirs` est créé
à partir de cette version nettoyée, sans nouvelle fonctionnalité SIRS.

Les anciennes installations QGIS locales sont sauvegardées séparément ; le profil
Ubuntu reçoit `sig_prestations` 0.8.0 et `lidar_hd_2026` 0.1.3. L’installation
expérimentale distincte est retirée. Les connexions PostgreSQL restent inchangées.
La validation Windows native reste réservée.
