# LiDAR HD 2026 — plugin QGIS 0.1.3

Extension autonome pour extraire le MNT LiDAR HD IGN à partir de polygones.
Aucune installation de Python ou de bibliothèque supplémentaire n'est nécessaire
pour l'utilisateur : QGIS fournit PyQGIS, Qt et GDAL. Le plugin ne dépend pas de
SIG-Prestations. Le script de référence `lidar_ign.py` reste inchangé et n'est
ni importé ni inclus dans le ZIP.

## Installation et utilisation

1. Dans QGIS, ouvrir **Extensions → Installer/Gérer les extensions → Installer
   depuis un ZIP** et choisir `plugin/dist/lidar_hd_2026-0.1.3.zip` dans le dépôt.
2. Activer **LiDAR HD 2026**. Son unique bouton se trouve dans la barre
   **SIG-Milieux**. Si nécessaire, afficher cette barre via
   **Vue → Barres d'outils**. Le gestionnaire d'extensions reste accessible normalement.
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
6. Choisir la **bibliothèque LiDAR permanente**, existante et inscriptible, puis
   un nom pour le VRT individuel. Le chemin local est mémorisé. Réutiliser la même
   bibliothèque évite les copies de tuiles entre extractions. Les noms réservés
   sous Windows, les VRT existants et les anciens dossiers homonymes sont refusés.
7. Lire le **prédiagnostic sans requête IGN** : entités, parties, surface cumulée
   après tampon, blocs dans la bibliothèque, blocs disponibles seulement dans
   le cache technique, téléchargements IGN et nouvelles données à enregistrer.
   L'estimation initiale vaut 1 Mio par bloc, puis utilise la taille moyenne des
   blocs valides concernés déjà disponibles. Il s'agit d'une estimation, pas
   d'un devis ni d'une garantie de taille des réponses WMS.
8. Cliquer **Extraire le MNT** et suivre la progression dans le dialogue ou le
   gestionnaire de tâches QGIS. **Annuler la tâche** conserve les blocs valides
   dans le cache ; une nouvelle extraction reprend sans les télécharger.
9. Le VRT est automatiquement ajouté au projet, avec Turbo, interpolation
   linéaire, min/max estimés sur la vue actualisée (**Updated Canvas**). Si la
   symbologie échoue, le raster demeure chargé avec son rendu par défaut et
   l'erreur est affichée. Après réussite complète, une notification QGIS confirme
   l'extraction et le dialogue se ferme automatiquement. Il reste ouvert en cas
   d'annulation, d'échec, de MNT incomplet ou de problème de symbologie. Un clic
   sur l'icône rouvre le dialogue avec la progression remise à zéro.

Après une mise à jour, redémarrer QGIS et vérifier la version et le chemin
chargés dans le journal **LiDAR HD 2026**. Pour la barre commune avec
SIG-Prestations, installer également son ZIP courant : les anciennes versions
utilisant la barre générale des extensions ne rejoignent pas cette barre.
La barre **SIG-Milieux** est affichée à l'activation, avec une icône par plugin ;
aucune entrée LiDAR n'est ajoutée au menu Extensions.

Les paramètres sont disposés dans les deux colonnes **Emprise** et **Fichiers**.
Les infobulles expliquent les options. Sur un écran réduit ou à forte échelle,
le contenu peut défiler ; les commandes et la progression restent accessibles.

En cas d'échecs de blocs, un MNT **INCOMPLET** peut être ajouté. Le message indique
les blocs manquants ; relancer avec un autre nom pour compléter depuis le cache.
Enregistrer le projet QGIS pour conserver la couche et sa symbologie.
Les blocs sont rectangulaires : les pixels hors polygones ne sont pas masqués.

## Fichiers et stockage

Les modes **Disque local** et **Pool commun** utilisent la même organisation :

```text
LiDAR HD IGN 2026/                  # bibliothèque choisie
  tiles/
    mnt_l93_<x>_<y>_256m_512px.tif   # une copie par bibliothèque
  extractions/
    site_A.vrt                     # sources relatives ../tiles/...
    site_B.vrt
  mnt_lidar_global.vrt              # sources relatives tiles/...
```

Le champ **Bibliothèque** désigne directement ce dossier permanent, pas un
nouveau dossier à chaque extraction. Il est mémorisé par `QSettings` après un
choix valide ou l'utilisation du chemin saisi. Un dossier vide est autorisé ;
`tiles/` et `extractions/` sont créés lors de l'extraction. L'interface distingue
un dossier vide, une bibliothèque déjà structurée et un dossier existant à
compléter. Les fichiers existants restent en place.

Chaque VRT individuel référence **seulement les blocs de l'emprise demandée**,
tampon compris. Il est le seul raster chargé automatiquement dans QGIS, avec
Turbo/Updated Canvas. Il ne contient pas toutes les tuiles de la bibliothèque.
Les tuiles déjà présentes sont prioritaires, puis celles du cache technique ;
seuls les blocs encore absents sont téléchargés. Les nouvelles tuiles sont
copiées une fois vers `tiles/`, même si plusieurs extractions les utilisent.

Le VRT global est un contrôle discret de la couverture cumulée. Il inventorie
les GeoTIFF valides réellement présents dans `tiles/`, indépendamment des VRT
individuels, et n'est jamais chargé automatiquement. Après chaque extraction,
sa liste et sa signature de sources sont vérifiées ; l'inventaire validé
reste dans ses métadonnées, sans fichier annexe. Les tuiles nouvelles ou modifiées
(taille/date) sont recontrôlées ; le VRT n'est réécrit que si nécessaire. Le bouton
**Actualiser le VRT global** force le contrôle intégral des GeoTIFF et prend
en compte les ajouts, retraits ou remplacements manuels, sans polygone requis
et sans téléchargement. Les fichiers invalides sont ignorés avec un avertissement,
sans être supprimés. Si aucune tuile valide ne reste, le VRT global périmé est
retiré ; aucun GeoTIFF n'est effacé.

L'inventaire et la validation du global s'exécutent dans une tâche QGIS,
annulable, hors du thread graphique. Le contrôle automatique réutilise les
validations dont la taille et la date n'ont pas changé ; le bouton manuel
relit tous les pixels, même si ces métadonnées sont inchangées. Les tuiles
utilisées par une extraction sont toujours validées intégralement. La première
création du global et sa reconstruction relisent les sources : leur coût
augmente avec la bibliothèque, surtout sur un partage réseau. Une erreur de
mise à jour conserve le VRT global précédent et le VRT individuel réussi ;
elle est signalée dans la barre de messages.
Une annulation pendant le contrôle global conserve également l'individuel déjà
enregistré et laisse le dialogue ouvert.

Pour déplacer ou partager les résultats, conserver **toute la bibliothèque**,
avec `tiles/` et `extractions/`. Copier uniquement un VRT individuel ne suffit
pas. Un projet `.qgz` enregistré dans la bibliothèque avec des chemins relatifs
peut être déplacé avec elle. Les blocs techniques restent dans
`~/qgis_lidar_ign_cache/tiles/`, comme dans le script historique : ce cache évite
les téléchargements déjà réalisés, même après un changement de bibliothèque.
Il n'est pas nécessaire pour rouvrir une bibliothèque exportée.

Le changement de bibliothèque locale est libre ; l'interface avertit du risque
de duplication entre bibliothèques distinctes. Aucune fusion, aucun déplacement
et aucune suppression automatique ne sont effectués. Les anciennes extractions
locales `<dossier>/<nom>/tiles/` et celles du pool
`extractions/<nom>/<nom>.vrt` sont conservées telles quelles. Les noms de leurs
dossiers sont réservés pour éviter toute ambiguïté avec un nouveau VRT.

Le pool Windows conserve exactement son chemin configuré :

```text
J:\1-milieux-aquatiques-et-risques\0. QGIS\Catalogues des couches\LiDAR HD IGN 2026
```

`POOL_ROOT` dans `lidar_hd_2026/config.py` vaut `None` sur les autres systèmes ;
le mode est alors désactivé. Le dossier partagé doit être accessible en lecture
et écriture. Les fichiers sont publiés par remplacement atomique depuis un
temporaire du même dossier, sous verrous `QLockFile` : un verrou par tuile,
un par nom d'extraction, et un pour le VRT global. L'ordre bibliothèque puis
cache empêche deux utilisateurs dotés de caches distincts de télécharger le
même bloc simultanément. L'attente des verrous est annulable et bornée à dix
minutes. Les verrous d'extraction et du global ne deviennent pas périmés
sur la seule durée d'un traitement long. Les noms existants sont revérifiés
sous verrou avant toute publication.

Aucune tuile mutualisée n'est supprimée par le plugin. Supprimer un VRT ou
retirer sa couche QGIS ne retire pas ses GeoTIFF. Un partage inaccessible produit
une erreur explicite. Le comportement des verrous et renommages doit encore
être validé sur le véritable partage SMB et sous Windows.
Une suppression manuelle de tuile peut rendre plusieurs VRT individuels
incomplets ; actualiser le global ne restaure pas les fichiers supprimés.

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
# Mesures locales reproductibles, 100 tuiles synthétiques, résultats sous audits/ :
QT_QPA_PLATFORM=offscreen /usr/bin/python3 LiDAR/tests/benchmark_library.py
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
`audits/LIDAR-PLUGIN-04.md` pour le stockage mutualisé et le VRT global,
`audits/LIDAR-PLUGIN-03.md` pour la finalisation du dialogue et
`audits/LIDAR-PLUGIN-02.md` pour les résultats effectifs de la correction IGN,
de la barre et du dialogue ; `audits/LIDAR-PLUGIN-01.md` décrit
l'inventaire des API. La validation Windows et les recettes interactives dans
QGIS Desktop 3.34/3.40 restent à réaliser ; les tests PyQGIS hors écran ont été
exécutés réellement sur ces versions sous Linux.
