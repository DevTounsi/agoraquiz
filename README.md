# AgoraQuiz Bot

Application Windows autonome avec interface graphique (Edge WebView2) pour automatiser les QCM **Aventure** et **Duels (Affrontements)** sur [AgoraQuiz](https://play.agoraquiz.education), basée sur [gauthierstrich/agoraquiz v1.0.0](https://github.com/gauthierstrich/agoraquiz/releases/tag/v1.0.0).

## Prérequis obligatoires (pour utiliser `AgoraQuizBot.exe`)

1. **Windows 10 / 11** (avec **Microsoft Edge** ou **Google Chrome** installé — Edge est inclus par défaut sur Windows, rien de plus à installer pour le navigateur).
2. **`agy` CLI installé dans le `PATH` et connecté** dans le terminal :
   - Vérifiez dans PowerShell que `agy` répond et est bien authentifié (`agy models`).

## Utilisation rapide (Release)

1. Téléchargez `AgoraQuizBot.exe` depuis la page **Releases** GitHub.
2. Double-cliquez sur `AgoraQuizBot.exe` (inutile d'installer Python).
3. Dans l'interface :
   - Renseignez votre **identifiant** et **mot de passe** AgoraQuiz (sauvegardés localement dans `config.json` à côté de l'`.exe`).
   - Choisissez le **Mode** (`Aventure` ou `Duels (Affrontements)`), le **Nombre de parties** (`0` = jusqu'à épuisement des tickets) et éventuellement un **Thème ciblé**.
   - Sélectionnez le **Modèle** et l'**Effort** de raisonnement.
   - Réglez le **Taux de convergence de réussite** (par défaut `80%`).
   - Cochez ou décochez **Afficher la fenêtre Chromium**, puis cliquez sur **Lancer le bot**.

## Fonctionnalités

- **Connexion automatique** sur `/auth` et conservation de la session dans `.pw_profile/`.
- **Mode Aventure** : ouvre `/play/adventure`, repère le nœud `CONTINUER` ou `COMMENCER ICI`, vérifie les tickets disponibles et résout les questions.
- **Mode Duels (Affrontements)** : ouvre `/play/battle`, reprend une manche en cours (`Jouer`) ou cherche un adversaire, sélectionne le thème puis résout la manche jusqu'à l'écran de fin.
- **Taux de convergence configurable** : régule dynamiquement les bonnes réponses et erreurs volontaires pour converger vers le pourcentage choisi (80 % par défaut).

## Développement & compilation depuis les sources

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\playwright install chromium
.venv\Scripts\python agora_gui.py
```

Compiler l'exécutable :

```powershell
.venv\Scripts\pyinstaller --onefile --noconsole --name AgoraQuizBot --add-data "ui;ui" --collect-all playwright --collect-all webview agora_gui.py
```
