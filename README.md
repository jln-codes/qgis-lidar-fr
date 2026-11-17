# LiDAR HD 2026 — plugin QGIS 0.1.1

Extension autonome pour extraire le MNT LiDAR HD IGN à partir de polygones.
Aucune installation de Python ou de bibliothèque supplémentaire n'est nécessaire
pour l'utilisateur : QGIS fournit PyQGIS, Qt et GDAL. Le plugin ne dépend pas de
SIG-Prestations. Le script de référence `lidar_ign.py` reste inchangé et n'est
ni importé ni inclus dans le ZIP.

## Installation et utilisation

1. Dans QGIS, ouvrir **Extensions → Installer/Gérer les extensions → Installer
   depuis un ZIP** et choisir `plugin/dist/lidar_hd_2026-0.1.1.zip` dans le dépôt.
2. Activer **LiDAR HD 2026**. Son unique bouton se trouve dans **SIG-Milieux**,
   également accessible par **Extensions → SIG-Milieux**. Si nécessaire, afficher
   cette barre via **Vue → Barres d'outils**.
3. Charger une couche Polygon ou MultiPolygon avec un SCR correctement déclaré.
4. Choisir **Toutes les entités** pour utiliser toute la couche, quelle que soit
   la sélection QGIS existante, ou **Sélectionner sur la carte**. Dans ce second
   mode, cliquer les polygones des couches polygonales visibles : clic gauche
   pour ajouter/retirer, clic droit pour terminer, Échap pour annuler la session
   de sélection. En cas de superposition, un menu permet de choisir explicitement
   la couche et l'entité. Plusieurs couches peuvent participer à une extraction.
   Le dialogue et l'outil cartographique précédent sont restaurés à la fin.
5. Régler le tampon (25 m par défaut, entre 0 et 100 m). Il augmente l'emprise
   après transformation en Lambert-93 ; les géométries sources restent intactes.
6. Choisir un nom et un dossier existant et inscriptible. Les noms réservés sous
   Windows sont refusés. Un dossier d'extraction existant n'est jamais écrasé :
   choisir un autre nom pour une nouvelle extraction.
7. Lire le **prédiagnostic sans requête IGN** : entités, parties, surface cumulée
   après tampon, blocs, cache valide, téléchargements, transferts IGN et copies.
   L'estimation initiale vaut 1 Mio par bloc, puis utilise la taille moyenne des
   blocs valides concernés déjà disponibles. Il s'agit d'une estimation, pas
   d'un devis ni d'une garantie de taille des réponses WMS.
8. Cliquer **Extraire le MNT** et suivre la progression dans le dialogue ou le
   gestionnaire de tâches QGIS. **Annuler la tâche** conserve les blocs valides
   dans le cache ; une nouvelle extraction reprend sans les télécharger.
9. Le VRT est automatiquement ajouté au projet, avec Turbo, interpolation
   linéaire, min/max estimés sur la vue actualisée (**Updated Canvas**). Si la
   symbologie échoue, le raster demeure chargé avec son rendu par défaut et
   l'erreur est affichée.

Après une mise à jour, redémarrer QGIS et vérifier la version et le chemin
chargés dans le journal **LiDAR HD 2026**. Pour la barre commune avec
SIG-Prestations, installer également son ZIP courant : les anciennes versions
utilisant la barre générale des extensions ne rejoignent pas cette barre.
La barre **SIG-Milieux** est affichée à l'activation, avec une icône par plugin ;
son entrée de menu conserve son libellé.

Les paramètres sont disposés dans les deux colonnes **Emprise** et **Fichiers**.
Les infobulles expliquent les options. Sur un écran réduit ou à forte échelle,
le contenu peut défiler ; les commandes et la progression restent accessibles.

En cas d'échecs de blocs, un MNT **INCOMPLET** peut être ajouté. Le message indique
les blocs manquants ; relancer avec un autre nom pour compléter depuis le cache.
Enregistrer le projet QGIS pour conserver la couche et sa symbologie.
Les blocs sont rectangulaires : les pixels hors polygones ne sont pas masqués.

## Fichiers et stockage

Le mode local recommandé produit un dossier autonome :

```text
<dossier choisi>/<nom>/
  <nom>.vrt
  tiles/
    mnt_l93_<x>_<y>_256m_512px.tif
```

Les sources du VRT sont relatives. Déplacer ou partager **tout le dossier**,
avec `tiles/`. Pour déplacer aussi un projet, enregistrer son `.qgz` dans ce
même dossier et choisir les chemins relatifs dans les propriétés du projet.
Les blocs techniques restent dans `~/qgis_lidar_ign_cache/tiles/`, comme dans le
script de référence ; ce cache n'est pas nécessaire à la réouverture d'un export
local. Ne pas supprimer le cache pendant une extraction.

Le **pool commun** est désactivé tant que `POOL_ROOT` dans
`lidar_hd_2026/config.py` vaut `None`. L'administrateur doit y renseigner le
**véritable chemin partagé**, avec `Path(...)`, puis reconstruire et réinstaller
le ZIP ; aucune adresse réseau n'est fournie par défaut. Le dossier partagé doit
exister et être accessible en lecture et écriture pour tous les utilisateurs.

```text
<pool>/
  tiles/                       # GeoTIFF mutualisés
  extractions/<nom>/<nom>.vrt   # noms uniques, sources ../../tiles/...
```

Les blocs déjà disponibles dans le pool évitent l'IGN et la copie. Ceux du cache
local sont copiés si nécessaire. Les fichiers partagés sont publiés par
renommage atomique, sous verrous Qt par bloc, avec temporaires uniques. Une tâche
attend un verrou au maximum 10 minutes, de manière annulable. La réservation du
dossier d'extraction est exclusive : deux utilisateurs utilisant le même nom
reçoivent un conflit, sans écrasement. Le plugin ne propose aucune suppression
des blocs communs. Retirer une couche QGIS ou supprimer son dossier sous
`extractions/` ne doit jamais entraîner la suppression de `tiles/`.
Un partage déconnecté produit une erreur explicite. Les sémantiques de verrou et
de renommage devront être validées sur le véritable partage SMB et sous Windows.

## Barre et icône

Les deux plugins recherchent la même barre Qt `SIGMilieuxToolbar`, titrée
**SIG-Milieux**, sans importer le code de l'autre. Chaque plugin retire seulement
ses propres actions ; le dernier retire la barre vide. Le ZIP SIG-Prestations
reconstruit avec cette adaptation graphique est aussi disponible sous `plugin/dist/`.
Son icône et ses fonctionnalités métier ne changent pas.

L'icône LiDAR reprend la référence fournie
`a_clean_flat_vector_style_icon_on_a_white_backgr.png` : cercle marine, scanner,
faisceaux bleus, vagues bleue, jaune et rouge. `icons/lidar_hd.svg` est un dessin
vectoriel natif, sans PNG incorporé, police ni ressource externe. L'intérieur du
cercle reste blanc ; l'extérieur est transparent. `icons/lidar_hd.png` est son
export 128 × 128. Le SVG est utilisé dans l'action et les métadonnées. Les
aperçus 16, 24, 32 et 128 pixels sur fonds clair/sombre sont générés dans
`audits/lidar-icons/`. La pastille précédente y reste consultable pour examen
visuel, sans entrer dans le ZIP.

## Construction et tests reproductibles

Depuis la racine du dépôt :

Les ZIP sont des sorties locales générées dans `plugin/dist/` et ignorées par
Git. Les commandes ci-dessous permettent de les reconstruire après un clone.

```sh
python3 LiDAR/build_plugin.py
# Construire aussi le ZIP SIG-Prestations courant dans plugin/dist/ :
python3 LiDAR/build_plugin.py --with-sig
# Régénérer PNG et aperçus avec le Python fourni avec QGIS :
QT_QPA_PLATFORM=offscreen /usr/bin/python3 LiDAR/build_plugin.py --icons
# Tests PyQGIS/GDAL, données synthétiques, réseau Python interdit :
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest discover -s LiDAR/tests -v
# Régression de l'intégration graphique SIG-Prestations :
QT_QPA_PLATFORM=offscreen /usr/bin/python3 -m unittest plugin.tests.test_package -v
QT_QPA_PLATFORM=offscreen /usr/bin/python3 plugin/tests/test_qgis_runtime.py
```

Sous Windows, exécuter `python LiDAR/build_plugin.py --icons` et
`python -m unittest discover -s LiDAR/tests -v` dans l'environnement Python/QGIS
fourni par l'installation QGIS. Le test configure `offscreen` par défaut.
Ne pas lancer avec un Python sans PyQGIS. Les sorties temporaires sont placées
sous `audits/` et nettoyées automatiquement. Le constructeur contrôle le manifeste
fermé du ZIP et l'identité binaire de chaque fichier avec les sources ; les
horodatages ZIP sont fixes pour rendre l'archive reproductible.

`tests/desktop_smoke.py` est une recette automatisée distincte, lancée dans
**QGIS Desktop réel**, avec les ZIP extraits dans un profil de test, des clics Qt
et des GeoTIFF fictifs. Sous Linux, après avoir construit les deux ZIP dans
`plugin/dist/`, on peut la lancer dans un affichage virtuel :

```sh
LIDAR_PROJECT_ROOT="$PWD" QT_QPA_PLATFORM=xcb QT_XCB_GL_INTEGRATION=none \
QT_OPENGL=software LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a qgis \
  --nologo --noversioncheck \
  --profiles-path "$PWD/audits/lidar-desktop/profiles" --profile lidar-smoke \
  --code "$PWD/LiDAR/tests/desktop_smoke.py"
```

Cette recette utilise un profil isolé, sans extensions utilisateur ; conserver
l'extension intégrée **Processing** active, nécessaire à l'exécution de `--code`.
Elle est distincte d'une recette humaine. Son résultat est écrit dans
`audits/lidar-desktop/result.json`. Les tests avec des façades d'énumérations
3.34 et 3.40 sont **simulés**, pas des exécutions sur ces versions. Consulter
`audits/LIDAR-PLUGIN-02.md` pour les résultats effectifs de la correction IGN,
de la barre et du dialogue ; `audits/LIDAR-PLUGIN-01.md` décrit
l'inventaire des API. La validation Windows et les recettes interactives dans
QGIS Desktop 3.34/3.40 restent à réaliser ; les tests PyQGIS hors écran ont été
exécutés réellement sur ces versions sous Linux.
