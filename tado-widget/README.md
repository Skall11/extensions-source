# Tado Widget

Petit widget de bureau (Windows / macOS / Linux) : températures, humidité, consigne,
puissance de chauffe, prochain changement, et programme hebdo de chaque pièce.

## Installation (Windows)

1. Installer Python 3 depuis https://python.org (cocher **Add Python to PATH** ; tkinter est inclus).
2. Copier `tado_widget.py` dans un dossier, par ex. `C:\Users\<toi>\tado\`.
3. Lancer : double-clic, ou `python tado_widget.py`.
4. Première fois : le navigateur s'ouvre sur login.tado.com → se connecter → valider le code affiché.
   Ensuite le widget se reconnecte tout seul (refresh token dans `%USERPROFILE%\.tado_widget\token.json`).

Sans fenêtre console : renommer en `tado_widget.pyw`.
Démarrage auto avec Windows : `Win+R` → `shell:startup` → y mettre un raccourci vers le `.pyw`.

## Utilisation

- 📌 : garder au premier plan
- ⟳ : rafraîchir maintenant
- clic sur une pièce : programme de la semaine (barre blanche = maintenant)
- position/taille de la fenêtre mémorisées

## Options

```
python tado_widget.py --demo            # aperçu sans compte
python tado_widget.py --interval 60     # rafraîchir toutes les 60 s
python tado_widget.py --home 123456     # choisir le logement si plusieurs
python tado_widget.py --logout          # oublier la connexion
```

## Quota API tado

Depuis 2025 tado limite l'API : ~100 requêtes/jour sans abonnement Auto-Assist,
~20 000 avec. Défaut = 1 rafraîchissement / 20 min (+ météo 1 fois sur 3) ≈ 100/jour.
Avec Auto-Assist : `--interval 60` sans souci. Le quota restant s'affiche en bas
quand tado le renvoie. Le programme d'une pièce est chargé une fois puis mis en cache.

## Notes

- API non officielle (celle de app.tado.com) : peut changer sans préavis.
- tado V3/V3+ : testé sur format de réponses connu. tado X (hops.tado.com) : support best-effort.
- Lecture seule : rien n'est modifié sur ton installation.
