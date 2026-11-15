# Tado Widget

Widget de bureau pour tado (tado X et V3/V3+) : voir **et régler** le chauffage.

- Températures, humidité, consigne, puissance de chauffe, fenêtre ouverte, prochain changement
- Régler la consigne d'une pièce (−/+, ON/OFF) avec durée : prochain créneau / 1 h / 3 h / permanent
- Reprendre le programme d'une pièce
- Modifier le programme hebdo (tado X) : découper / supprimer des créneaux, heures, températures,
  copier sur Lun–Ven / week-end / tous les jours
- Actions rapides (tado X) : boost partout, tout éteindre, reprendre partout
- Vrai widget : sans bordure, déplaçable, opacité réglable, toujours au premier plan (option),
  icône dans la barre système, lancement au démarrage de Windows (option)

## Installation (Windows)

### Option A — exe tout prêt
1. GitHub → repo → onglet **Actions** → workflow **Tado widget (Windows exe)** → dernier run vert →
   télécharger l'artefact **TadoWidget** (zip contenant `TadoWidget.exe`).
2. Mettre `TadoWidget.exe` où tu veux (ex. `C:\Users\<toi>\TadoWidget\`) et double-cliquer.
   Windows SmartScreen peut avertir (exe non signé) → « Informations complémentaires » → « Exécuter quand même ».

### Option B — compiler soi-même
1. Installer Python 3 depuis https://python.org (cocher **Add Python to PATH**).
2. Double-cliquer `build_exe.bat` → `dist\TadoWidget.exe`.

### Option C — sans exe
`pip install pystray pillow` puis `pythonw tado_widget.py` (pas de console avec `pythonw`).

## Première connexion
Le navigateur s'ouvre sur login.tado.com → se connecter → valider le code affiché dans le widget.
Ensuite reconnexion automatique (refresh token dans `%USERPROFILE%\.tado_widget\token.json`,
aucun mot de passe stocké).

## Utilisation
- Glisser le titre pour déplacer le widget
- Clic sur une pièce → fenêtre de réglage + programme ; clic sur un jour → éditeur du jour
- `⟳` rafraîchir · `⋯` ou clic droit → menu (actions rapides, premier plan, opacité,
  démarrage auto, déconnexion, quitter)
- `✕` masque le widget dans la barre système (clic sur l'icône pour le réafficher)

## Options
```
TadoWidget.exe --interval 60     # rafraîchir toutes les 60 s
TadoWidget.exe --home 123456     # choisir le logement si plusieurs
TadoWidget.exe --demo            # aperçu sans compte
TadoWidget.exe --logout          # oublier la connexion
```

## Quota API tado
tado limite l'API : ~100 requêtes/jour sans abonnement Auto-Assist, ~20 000 avec.
Chaque lecture **et** chaque réglage compte. Défaut = 1 rafraîchissement / 20 min (≈ 100/jour).
Enregistrer un programme = 1 requête par jour coché. Avec Auto-Assist : `--interval 60` sans souci.

## Notes
- API non officielle (celle de app.tado.com) : peut changer sans préavis.
- tado X : endpoints hops.tado.com vérifiés contre PyTado et les intégrations Home Assistant tado X.
- tado V3/V3+ : réglage consigne + reprise OK, programme en lecture seule.
