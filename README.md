# Autocut

Application Windows pour découper des pistes de bruitages (SFX) en sons séparés, les nommer et les exporter en WAV, MP3 ou OGG.

## Fonctionnalités

- **Une ou plusieurs pistes** : glisse des fichiers ou un dossier entier dans la fenêtre (WAV, MP3, OGG, FLAC, AIFF).
- **Détection automatique** des sons séparés par du silence, avec seuil, silence minimum, son minimum et marge réglables, et des **préréglages** (fournis ou enregistrés par toi).
- **Forme d'onde éditable** : glisser les bords d'un son, dessiner un nouveau son, couper, fusionner, supprimer, annuler (Ctrl+Z).
- **Écoute** d'un son d'un clic (ou Espace), son par son avec ↑/↓, tout à la suite (Ctrl+Espace), en option avec les réglages d'export appliqués.
- **Nommage** : un titre numéroté ou un nom par son (tapé dans le tableau ou collé sous forme de liste), avec un **modèle** commun comme `SFX_{titre}_{n}` (étiquettes `{titre}`, `{n}`, `{piste}`).
- **Export** en WAV (16, 24 ou 32 bits), MP3 ou OGG, avec en option : mono, fréquence (22,05 / 44,1 / 48 kHz), normalisation, fondus, un sous-dossier par piste et une **liste CSV** des sons (`liste_sons.csv`).
- **Sessions** : Ctrl+S enregistre le découpage dans un fichier `.autocut` pour le reprendre plus tard.
- Thème sombre, réglages mémorisés d'une session à l'autre.

## Télécharger le .exe

Chaque modification de la branche `main` fabrique `Autocut.exe` automatiquement sur GitHub :

1. Onglet **Actions** du dépôt, puis le dernier passage réussi de **Build Windows**.
2. En bas de la page, section **Artifacts**, télécharge **Autocut-windows** (un .zip contenant `Autocut.exe`).
3. Double-clique sur `Autocut.exe`. Si Windows SmartScreen affiche un avertissement (l'exe n'est pas signé), clique sur « Informations complémentaires » puis « Exécuter quand même ».

Pour une version stable, crée un tag `v0.1.0` : le .exe est alors publié dans l'onglet **Releases**.

## Fabriquer le .exe soi-même

1. Installe Python 3.10 ou plus depuis [python.org](https://www.python.org/downloads/) en cochant **Add python.exe to PATH**.
2. Double-clique sur `build.bat`. Le résultat est `dist\Autocut.exe`.

Pour lancer depuis les sources sans fabriquer l'exe : `lancer.bat`.

## Utilisation rapide

| Action | Comment |
| --- | --- |
| Écouter un son | Clic dessus, ou Espace |
| Son précédent / suivant | ↑ / ↓ |
| Tout écouter à la suite | Ctrl+Espace |
| Arrêter | Échap |
| Ajuster un son | Glisser un de ses bords |
| Créer un son | Glisser dans une zone vide |
| Couper / fusionner / supprimer | Clic droit sur le son |
| Zoom | Molette, double-clic sur un son, Ctrl+0 pour tout afficher |
| Défiler | Maj+molette, barre de défilement ou clic molette |
| Annuler / rétablir | Ctrl+Z / Ctrl+Y |

Réglages de détection, si le résultat ne convient pas :

- deux sons collés en un seul : baisse le **silence minimum** ;
- un son coupé en morceaux (pas, rebonds, échos) : augmente le **silence minimum** ;
- le bruit de fond est pris pour un son : monte le **seuil de silence** (par ex. -35 dB) ;
- la fin d'un son est coupée : augmente la **marge** ou baisse le seuil.

## Développement

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
python -m autocut
```

Le code : `autocut/core.py` (détection, nommage, export, sessions, sans interface), `autocut/waveform.py` (forme d'onde), `autocut/theme.py` (thème et icônes), `autocut/app.py` (fenêtre principale). L'icône de l'appli est dessinée par `tools/make_icon.py`.
