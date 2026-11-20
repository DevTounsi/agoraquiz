import argparse
import getpass
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time

if getattr(sys, "frozen", False) and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
    local_browsers = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "ms-playwright"
    if local_browsers.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)

from playwright.sync_api import sync_playwright

BASE_URL = "https://play.agoraquiz.education"
APP_VERSION = "1.2.0"
GITHUB_REPO = "DevTounsi/agoraquiz"
DEFAULT_BASE_MODEL = "gemini-4-hydrogen"
DEFAULT_MODEL = "gemini-4-hydrogen-low"
DEFAULT_EFFORT = "low"
DEFAULT_SUCCESS_RATE = 80
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max", "default")
FALLBACK_MODELS = [
    ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
    ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
    ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
    ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
    ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
    ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
    ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
    ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
    ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
    ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
    ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
    ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
    ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
    ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
    ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
    ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
]

QUESTION_SELECTOR = "#root div.max-w-2xl.text-balance.text-center"
OPTION_SELECTOR = "#root button[role='button'][type='button']:not([disabled])"
EXPECTED_RE = re.compile(r"(\d+)\s+réponses?\s+attendues?", re.IGNORECASE)
NEXT_BUTTON_NAMES = (
    "Continuer",
    "Suivant",
    "Question suivante",
    "Terminer",
    "Retour",
    "Retour aux affrontements",
)
DUEL_END_PATTERNS = (
    ("Victoire", re.compile(r"\bvictoire\b|\btu as gagné\b", re.IGNORECASE)),
    ("Défaite", re.compile(r"\bdéfaite\b|\btu as perdu\b", re.IGNORECASE)),
    ("Égalité", re.compile(r"\bégalité\b|\bmatch nul\b", re.IGNORECASE)),
    ("Manche terminée (en attente de l'adversaire)", re.compile(r"en attente de|tour de l'adversaire|manche terminée", re.IGNORECASE)),
    ("Partie terminée", re.compile(r"partie terminée|score final|résultat", re.IGNORECASE)),
)
SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
EFFORT_SUFFIX_RE = re.compile(r"-(low|medium|high|xhigh|max)$", re.IGNORECASE)
LABEL_EFFORT_RE = re.compile(r"\s*\((Low|Medium|High|XHigh|Max)\)\s*$", re.IGNORECASE)


def get_app_dir() -> Path:
    """Retourne un dossier accessible en écriture pour config.json et .pw_profile."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        try:
            probe = exe_dir / ".write_test"
            probe.touch()
            probe.unlink()
            return exe_dir
        except Exception:
            fallback = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgoraQuizBot"
            fallback.mkdir(parents=True, exist_ok=True)
            return fallback
    return Path(__file__).resolve().parent


def find_agy_bin() -> str | None:
    """Trouve l'exécutable agy dans le PATH ou les emplacements standards."""
    found = shutil.which("agy")
    if found:
        return found
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "agy" / "bin" / "agy.exe",
        Path.home() / ".local" / "bin" / "agy.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "agy" / "agy.exe",
    ]
    for cand in candidates:
        if cand.exists():
            return str(cand)
    return None


def read_config_raw(app_dir: Path) -> dict:
    """Lit config.json sans interaction terminal."""
    config_path = app_dir / "config.json"
    cfg = {"username": "", "password": "", "quiz_name": ""}
    if config_path.exists():
        try:
            cfg.update(json.loads(config_path.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(app_dir: Path, cfg: dict) -> Path:
    """Sauvegarde config.json dans app_dir."""
    config_path = app_dir / "config.json"
    config_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return config_path


def load_config(app_dir: Path) -> dict:
    """Charge config.json ou demande les identifiants dans le terminal."""
    cfg = read_config_raw(app_dir)
    changed = False
    if not cfg.get("username"):
        cfg["username"] = input("Identifiant AgoraQuiz : ").strip()
        changed = True
    if not cfg.get("password"):
        cfg["password"] = getpass.getpass("Mot de passe AgoraQuiz : ")
        changed = True
    if changed:
        path = save_config(app_dir, cfg)
        print(f"Identifiants sauvegardés dans {path}")
    return cfg


def fetch_agy_models() -> tuple[list[tuple[str, str]], str]:
    """Récupère la liste brute des modèles via `agy models` et un message de diagnostic."""
    agy_bin = find_agy_bin()
    if not agy_bin:
        return list(FALLBACK_MODELS), "ERREUR : CLI 'agy' introuvable sur ce PC (installez agy et connectez-vous)."

    try:
        res = subprocess.run(
            [agy_bin, "models"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=SUBPROCESS_FLAGS,
            timeout=12,
        )
        if res.returncode == 0:
            models = []
            for line in res.stdout.splitlines():
                if "\t" in line:
                    model_id, label = line.split("\t", 1)
                    model_id = model_id.strip()
                    label = label.strip()
                    if model_id:
                        models.append((model_id, label))
            if models:
                return models, f"agy prêt ({agy_bin}) — {len(models)} modèles chargés."
        err = (res.stderr or res.stdout or "").strip()
        return list(FALLBACK_MODELS), f"ATTENTION : 'agy models' a échoué ({err or 'code ' + str(res.returncode)}). Connectez-vous à agy."
    except Exception as e:
        return list(FALLBACK_MODELS), f"ATTENTION : impossible d'exécuter agy ({e})."


def fetch_grouped_models() -> tuple[list[dict], str]:
    """Regroupe les modèles par famille et retourne (groupes, diagnostic agy)."""
    raw_models, status_msg = fetch_agy_models()
    groups: dict[str, dict] = {}
    for model_id, label in raw_models:
        eff_match = EFFORT_SUFFIX_RE.search(model_id)
        effort = eff_match.group(1).lower() if eff_match else "default"
        base_id = EFFORT_SUFFIX_RE.sub("", model_id)
        base_label = LABEL_EFFORT_RE.sub("", label).strip()

        if base_id not in groups:
            groups[base_id] = {"id": base_id, "label": base_label, "efforts": {}}
        groups[base_id]["efforts"][effort] = model_id

    result = []
    for g in groups.values():
        ordered = {k: g["efforts"][k] for k in EFFORT_ORDER if k in g["efforts"]}
        for k, v in g["efforts"].items():
            if k not in ordered:
                ordered[k] = v
        g["efforts"] = ordered
        result.append(g)

    result.sort(key=lambda g: (0 if g["id"] == DEFAULT_BASE_MODEL else 1, g["label"]))
    return result, status_msg


def ensure_on_play_home(page, log=print) -> None:
    """Force la navigation vers la page d'accueil des modes (/play)."""
    if page.url.rstrip("/") != f"{BASE_URL}/play":
        page.goto(f"{BASE_URL}/play", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)
    log_ticket_status(page, log=log)


def ensure_logged_in(page, username: str, password: str, log=print) -> None:
    """Se connecte automatiquement sur /auth si la session n'est pas active puis va sur /play."""
    page.goto(f"{BASE_URL}/play", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2500)

    if "/auth" in page.url:
        log("Connexion automatique...")
        page.wait_for_selector("input", timeout=15000)
        page.get_by_placeholder("Identifiant").fill(username)
        page.get_by_placeholder("Mot de passe").fill(password)
        page.get_by_role("button", name="Se connecter").click()

        for _ in range(30):
            if "/auth" not in page.url:
                break
            page.wait_for_timeout(500)

        if "/auth" in page.url:
            raise RuntimeError("Échec de la connexion : vérifiez vos identifiants.")
        log(f"Connecté ({page.url})")
    else:
        log(f"Session déjà active ({page.url})")

    if page.url.rstrip("/") != f"{BASE_URL}/play":
        page.goto(f"{BASE_URL}/play", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)


def log_ticket_status(page, log=print) -> None:
    """Affiche les compteurs de tickets lus sur /play s'ils sont visibles."""
    try:
        text = page.locator("body").inner_text(timeout=1000)
    except Exception:
        return
    adv = re.search(r"Tickets aventure\s*:\s*(\d+)", text, re.IGNORECASE)
    duel = re.search(r"Tickets affrontements\s*:\s*(\d+)", text, re.IGNORECASE)
    solo = re.search(r"Tickets défis solo\s*:\s*(\d+)", text, re.IGNORECASE)
    if adv or duel or solo:
        parts = []
        if adv:
            parts.append(f"Aventure: {adv.group(1)}")
        if duel:
            parts.append(f"Affrontements: {duel.group(1)}")
        if solo:
            parts.append(f"Solo: {solo.group(1)}")
        log("Tickets disponibles -> " + " | ".join(parts))


def should_answer_correctly(tracker: dict, target_rate: int) -> bool:
    """Décide si la question doit être réussie pour converger vers target_rate (%)."""
    r = max(0.0, min(1.0, target_rate / 100.0))
    if r >= 1.0:
        return True
    if r <= 0.0:
        return False

    total = tracker["total"]
    correct = tracker["correct"]
    expected_correct = (total + 1) * r
    deficit = expected_correct - correct
    if deficit >= 1.0:
        return True
    if deficit <= -1.0:
        return False

    prob = min(0.95, max(0.05, r + 0.35 * deficit))
    return random.random() < prob


def pick_wrong_indices(options: list[str], good_indices: list[int], expected_count: int) -> list[int]:
    """Choisit des index incorrects (en privilégiant les options non vides)."""
    non_empty_bad = [i for i, opt in enumerate(options) if i not in good_indices and opt.strip()]
    any_bad = [i for i in range(len(options)) if i not in good_indices]
    pool = non_empty_bad if len(non_empty_bad) >= expected_count else any_bad
    if len(pool) >= expected_count:
        return random.sample(pool, expected_count)
    # Fallback si toutes les options sont requises ou insuffisantes
    chosen = list(pool)
    for i in range(len(options)):
        if i not in chosen:
            chosen.append(i)
        if len(chosen) >= expected_count:
            break
    return chosen[:expected_count]


def select_and_start_adventure(page, quiz_name: str = "", log=print) -> bool:
    """Navigue depuis /play vers Aventure, sélectionne un quiz et clique sur Commencer."""
    if "/play/adventure" not in page.url:
        ensure_on_play_home(page, log=log)
        log("Ouverture du mode Aventure...")
        page.get_by_text("Aventure", exact=True).first.click(timeout=15000)
        page.wait_for_timeout(2500)

    result = page.evaluate(
        """(targetName) => {
            const badges = Array.from(document.querySelectorAll('div')).filter(
                d => d.children.length === 0 &&
                     (d.innerText.trim().toUpperCase() === 'CONTINUER' ||
                      d.innerText.trim().toUpperCase() === 'COMMENCER ICI')
            );
            if (!badges.length) return { ok: false, reason: 'NO_BADGE' };

            let chosen = null;
            if (targetName) {
                const needle = targetName.toLowerCase();
                for (const b of badges) {
                    let cur = b;
                    for (let i = 0; i < 8 && cur; i++) {
                        if ((cur.innerText || '').toLowerCase().includes(needle)) {
                            chosen = b;
                            break;
                        }
                        cur = cur.parentElement;
                    }
                    if (chosen) break;
                }
            }
            if (!chosen) {
                chosen = badges.find(b => b.innerText.trim().toUpperCase() === 'CONTINUER') || badges[0];
            }

            const container = chosen.closest('.relative');
            const btn = container ? container.querySelector('div[tabindex="0"]') : null;
            if (!btn) return { ok: false, reason: 'NO_BUTTON' };
            btn.click();
            return { ok: true, badge: chosen.innerText.trim() };
        }""",
        quiz_name,
    )

    if not result.get("ok"):
        log(f"Aucun quiz Aventure disponible trouvé ({result.get('reason')}).")
        return False

    log(f"Nœud sélectionné ({result.get('badge')}), ouverture du quiz...")
    page.wait_for_timeout(1500)

    req_btn = page.locator("button", has_text="REQUIS POUR CONTINUER")
    if req_btn.count() > 0 and req_btn.first.is_visible():
        log("Plus de tickets aventure (1 ticket requis pour continuer).")
        return False

    start_btn = page.locator("button", has_text="Commencer").first
    try:
        start_btn.wait_for(state="visible", timeout=5000)
    except Exception:
        log("Impossible de démarrer : bouton 'Commencer' introuvable (plus de tickets ?).")
        return False

    if start_btn.is_disabled():
        log("Le bouton 'Commencer' est désactivé (plus de tickets aventure ?).")
        return False

    start_btn.click()
    for _ in range(20):
        if "/adventure-game/" in page.url:
            break
        page.wait_for_timeout(500)

    log(f"Quiz démarré : {page.url}")
    return "/adventure-game/" in page.url


def select_and_start_duel(page, log=print) -> bool:
    """Navigue depuis /play vers Affrontements et lance ou rejoint un duel."""
    if "/play/battle" not in page.url:
        ensure_on_play_home(page, log=log)
        log("Ouverture du mode Affrontements (Duels)...")
        page.get_by_text("Affrontements", exact=True).first.click(timeout=15000)
        page.wait_for_timeout(2500)

    # 1. Vérifier s'il y a déjà un duel en cours jouable (Jouer / Continuer)
    for label in ("Jouer", "Continuer", "À ton tour"):
        btn = page.locator("button", has_text=label)
        if btn.count() > 0 and btn.first.is_visible() and not btn.first.is_disabled():
            log(f"Duel en cours trouvé ({label}), reprise...")
            btn.first.click()
            break
    else:
        # 2. Sinon lancer un nouvel affrontement
        find_btn = page.locator("button", has_text="Trouver un adversaire").first
        find_btn.wait_for(state="visible", timeout=10000)
        if find_btn.is_disabled():
            log("Bouton 'Trouver un adversaire' désactivé (plus de tickets affrontements ?).")
            return False
        log("Recherche d'un adversaire...")
        find_btn.click()

    for _ in range(30):
        if "/play/battle" not in page.url:
            break
        page.wait_for_timeout(500)

    if "/play/battle" in page.url:
        log("Impossible de démarrer le duel (timeout).")
        return False

    log(f"Duel démarré : {page.url}")
    return True


def select_and_start_solo(page, log=print) -> bool:
    """Navigue depuis /play vers Défis solo et lance un défi solo."""
    if "/play/solo" not in page.url:
        ensure_on_play_home(page, log=log)
        log("Ouverture du mode Défis solo...")
        page.get_by_text("Défis solo", exact=True).first.click(timeout=15000)
        page.wait_for_timeout(2500)

    start_btn = page.locator("button", has_text="Lancer un défi solo").first
    start_btn.wait_for(state="visible", timeout=10000)
    if start_btn.is_disabled():
        log("Bouton 'Lancer un défi solo' désactivé (plus de tickets défis solo ?).")
        return False

    log("Lancement du défi solo...")
    start_btn.click()

    for _ in range(30):
        if "/solo-game/" in page.url:
            break
        page.wait_for_timeout(500)

    if "/solo-game/" not in page.url:
        log("Impossible de démarrer le défi solo (timeout).")
        return False

    log(f"Défi solo démarré : {page.url}")
    return True


def is_in_game_url(url: str) -> bool:
    """Indique si l'URL correspond à une partie active (Aventure, Duel ou Solo)."""
    clean = url.rstrip("/")
    return (
        "/adventure-game/" in clean
        or "/battle-game/" in clean
        or "/solo-game/" in clean
    )


def get_expected_count(page) -> int:
    """Lit le nombre de réponses attendues dans la barre du bas (défaut: 1)."""
    try:
        text = page.locator("#root").inner_text(timeout=1000)
        match = EXPECTED_RE.search(text)
        if match:
            return max(1, int(match.group(1)))
    except Exception:
        pass
    return 1


def cleanup_agy_conversation(conversation_id: str) -> None:
    """Supprime la conversation éphémère créée par `agy -p` du stockage local."""
    if not conversation_id or not re.fullmatch(r"[0-9a-fA-F-]+", conversation_id):
        return

    cli_dir = Path(
        os.environ.get("ANTIGRAVITY_CLI_DATA_DIR")
        or (Path.home() / ".gemini" / "antigravity-cli")
    )
    if not cli_dir.exists():
        return

    shutil.rmtree(cli_dir / "brain" / conversation_id, ignore_errors=True)
    for rel in (
        f"conversations/{conversation_id}.db",
        f"conversations/{conversation_id}.db-wal",
        f"conversations/{conversation_id}.db-shm",
        f"presence/{conversation_id}.lock",
    ):
        try:
            (cli_dir / rel).unlink(missing_ok=True)
        except Exception:
            pass

    db_path = cli_dir / "conversation_summaries.db"
    if db_path.exists():
        try:
            con = sqlite3.connect(db_path, timeout=3)
            for table in ("conversation_summaries", "conversation_parents", "conversations_fts"):
                try:
                    con.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conversation_id,))
                except Exception:
                    pass
            con.commit()
            con.close()
        except Exception:
            pass


def ask_agy(
    question: str,
    options: list[str],
    expected_count: int,
    model: str,
    effort: str,
    ephemeral: bool = True,
    log=print,
) -> list[int]:
    """Envoie la question et les propositions à agy et retourne les index choisis."""
    formatted_options = "\n".join(f"{i}: {opt}" for i, opt in enumerate(options))
    if expected_count == 1:
        instruction = (
            "Réponds UNIQUEMENT par le chiffre (0, 1, 2, ...) de la bonne réponse, "
            "sans aucun autre texte."
        )
    else:
        instruction = (
            f"Il y a {expected_count} bonnes réponses. Réponds UNIQUEMENT par les "
            f"{expected_count} chiffres séparés par des virgules (ex: 0, 2), sans autre texte."
        )

    prompt = (
        "Tu es un assistant pour un QCM de POO Java et UML.\n"
        f"{instruction}\n\n"
        f"Question :\n{question}\n\n"
        f"Propositions :\n{formatted_options}"
    )

    agy_bin = find_agy_bin()
    if not agy_bin:
        log("Erreur : 'agy' est introuvable. Installez agy CLI et connectez-vous.")
        return []

    cmd = [
        agy_bin,
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "json",
        "--disable-slash-commands",
    ]
    if effort and effort != "default":
        cmd.extend(["--effort", effort])

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=SUBPROCESS_FLAGS,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        log("Erreur agy : délai dépassé (60s).")
        return []

    if result.returncode != 0:
        log(f"Erreur agy : {result.stderr.strip()}")
        return []

    raw_out = result.stdout.strip()
    output = raw_out
    conv_id = ""
    try:
        data = json.loads(raw_out)
        if isinstance(data, dict):
            conv_id = str(data.get("conversation_id", ""))
            if "response" in data:
                output = str(data["response"]).strip()
    except Exception:
        pass

    if ephemeral and conv_id:
        cleanup_agy_conversation(conv_id)

    indices = []
    for m in re.findall(r"\b(\d+)\b", output):
        idx = int(m)
        if 0 <= idx < len(options) and idx not in indices:
            indices.append(idx)

    if not indices:
        log(f"Réponse inattendue de agy : {output!r}")
        return []

    return indices[:expected_count]


def try_click_next(page) -> bool:
    """Clique sur un éventuel bouton Continuer/Suivant/Terminer/Retour après validation."""
    for label in NEXT_BUTTON_NAMES:
        locator = page.get_by_text(label, exact=True)
        try:
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click()
                return True
        except Exception:
            pass
    return False


def detect_end_screen(page) -> str | None:
    """Détecte un écran de fin de partie / fin de manche (notamment en Duel)."""
    try:
        text = page.locator("#root").inner_text(timeout=800)
    except Exception:
        return None
    for label, pattern in DUEL_END_PATTERNS:
        if pattern.search(text):
            return label
    return None


def handle_pregame_choice(
    page,
    option_locs,
    quiz_name: str = "",
    solo_type: str = "Premier à 10",
    log=print,
) -> bool:
    """Gère les écrans 'Choisir un thème' (Duel/Solo) et 'Choisir un mode de jeu' (Solo)."""
    try:
        root_text = page.locator("#root").inner_text(timeout=800)
    except Exception:
        return False

    lower = root_text.lower()
    if option_locs.count() == 0:
        return False

    if "choisir un thème" in lower:
        chosen_idx = 0
        if quiz_name:
            needle = quiz_name.lower()
            for i in range(option_locs.count()):
                if needle in option_locs.nth(i).inner_text().lower():
                    chosen_idx = i
                    break
        theme_text = option_locs.nth(chosen_idx).inner_text().strip()
        log(f"Sélection du thème : {theme_text}")
        option_locs.nth(chosen_idx).click()
        time.sleep(0.4)
        try_click_next(page)
        time.sleep(1.0)
        return True

    if "choisir un mode de jeu" in lower:
        chosen_idx = 0
        needle = (solo_type or "Premier à 10").lower()
        for i in range(option_locs.count()):
            if needle in option_locs.nth(i).inner_text().lower():
                chosen_idx = i
                break
        mode_text = option_locs.nth(chosen_idx).inner_text().strip()
        log(f"Sélection du défi solo : {mode_text}")
        option_locs.nth(chosen_idx).click()
        time.sleep(0.4)
        try_click_next(page)
        time.sleep(1.0)
        return True

    return False


def solve_current_quiz(
    page,
    model: str,
    effort: str,
    mode: str = "adventure",
    quiz_name: str = "",
    solo_type: str = "Premier à 10",
    success_rate: int = DEFAULT_SUCCESS_RATE,
    ephemeral: bool = True,
    tracker: dict | None = None,
    log=print,
    stop_event: threading.Event | None = None,
) -> bool:
    """Boucle de résolution jusqu'à la sortie de la partie. Retourne False si arrêté."""
    if tracker is None:
        tracker = {"correct": 0, "total": 0}
    last_signature = None
    no_question_ticks = 0
    idle_ticks = 0
    while is_in_game_url(page.url):
        if stop_event and stop_event.is_set():
            log("Arrêt demandé par l'utilisateur.")
            return False

        question_loc = page.locator(QUESTION_SELECTOR)
        option_locs = page.locator(OPTION_SELECTOR)

        if question_loc.count() == 0 or option_locs.count() == 0:
            if handle_pregame_choice(
                page, option_locs, quiz_name=quiz_name, solo_type=solo_type, log=log
            ):
                no_question_ticks = 0
                idle_ticks = 0
                continue

            end_label = detect_end_screen(page)
            if end_label:
                no_question_ticks += 1
                try_click_next(page)
                time.sleep(0.8)
                if not is_in_game_url(page.url) or no_question_ticks >= 3:
                    prefix = {"duel": "Duel terminé", "solo": "Défi solo terminé"}.get(
                        mode, "Quiz terminé"
                    )
                    log(f"\n>>> {prefix} : {end_label} <<<")
                    return True
                continue

            no_question_ticks = 0
            if try_click_next(page):
                idle_ticks = 0
                time.sleep(0.8)
            else:
                idle_ticks += 1
                if idle_ticks in (10, 30):
                    log(f"En attente d'une question sur {page.url}...")
                time.sleep(0.5)
            continue

        no_question_ticks = 0
        idle_ticks = 0

        question = question_loc.first.inner_text().strip()
        count = option_locs.count()
        options = [option_locs.nth(i).inner_text().strip() for i in range(count)]
        signature = (question, tuple(options))

        if not question or signature == last_signature:
            if try_click_next(page):
                time.sleep(0.8)
            else:
                time.sleep(0.5)
            continue

        expected_count = get_expected_count(page)
        log(f"\nQuestion ({expected_count} rép. attendue(s)) : {question}")
        for i, opt in enumerate(options):
            log(f"  [{i}] {opt}")

        good_indices = ask_agy(
            question, options, expected_count, model, effort, ephemeral=ephemeral, log=log
        )
        if good_indices:
            aim_correct = should_answer_correctly(tracker, success_rate)
            if aim_correct:
                indices = good_indices
                tracker["correct"] += 1
            else:
                indices = pick_wrong_indices(options, good_indices, expected_count)
            tracker["total"] += 1
            pct = (tracker["correct"] / tracker["total"]) * 100
            tag = "réponse visée juste" if aim_correct else "erreur volontaire"
            log(f"[{tag} | convergence {tracker['correct']}/{tracker['total']} = {pct:.0f}% (cible {success_rate}%)]")

            try:
                for idx in indices:
                    log(f"-> Clic sur [{idx}] {options[idx]}")
                    option_locs.nth(idx).click(timeout=4000)
                    time.sleep(0.1)

                valider = page.get_by_text("Valider", exact=True)
                if valider.count() > 0:
                    valider.first.click(timeout=4000)
                    log("-> Clic sur Valider")

                last_signature = signature
            except Exception as e:
                log(f"-> Écran modifié avant validation ({e}), passage à la suite...")
                try_click_next(page)
        else:
            log("-> Impossible de déterminer la réponse, nouvelle tentative dans 2s.")

        time.sleep(0.6 if mode == "solo" else 1.2)

    log("Partie terminée !")
    return True


def launch_browser_context(p, profile_dir: Path, headless: bool, log=print):
    """Lance Chromium Playwright ou bascule vers Edge/Chrome installé sur Windows."""
    try:
        return p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
        )
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
        for channel, label in (("msedge", "Microsoft Edge"), ("chrome", "Google Chrome")):
            try:
                log(f"Chromium Playwright absent, utilisation de {label}...")
                return p.chromium.launch_persistent_context(
                    user_data_dir=str(profile_dir),
                    headless=headless,
                    channel=channel,
                )
            except Exception:
                continue
        raise RuntimeError(
            "Aucun navigateur compatible trouvé (Chromium Playwright, Edge ou Chrome)."
        ) from e


def run_bot(
    username: str,
    password: str,
    mode: str = "adventure",
    model: str = DEFAULT_MODEL,
    effort: str = DEFAULT_EFFORT,
    headless: bool = False,
    count: int = 1,
    quiz_name: str = "",
    solo_type: str = "Premier à 10",
    success_rate: int = DEFAULT_SUCCESS_RATE,
    ephemeral: bool = True,
    direct_url: str = "",
    app_dir: Path | None = None,
    log=print,
    stop_event: threading.Event | None = None,
) -> int:
    """Exécute la session complète du bot (adventure, duel ou solo) et retourne le nombre de parties."""
    base_dir = app_dir or get_app_dir()
    profile_dir = base_dir / ".pw_profile"
    profile_dir.mkdir(exist_ok=True)
    done = 0
    tracker = {"correct": 0, "total": 0}
    rate = max(0, min(100, int(success_rate)))

    with sync_playwright() as p:
        context = launch_browser_context(p, profile_dir, headless=headless, log=log)
        page = context.pages[0] if context.pages else context.new_page()

        try:
            ensure_logged_in(page, username, password, log=log)

            if direct_url:
                page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)
                if solve_current_quiz(
                    page,
                    model,
                    effort,
                    mode=mode,
                    quiz_name=quiz_name,
                    solo_type=solo_type,
                    success_rate=rate,
                    ephemeral=ephemeral,
                    tracker=tracker,
                    log=log,
                    stop_event=stop_event,
                ):
                    done = 1
            else:
                while count == 0 or done < count:
                    if stop_event and stop_event.is_set():
                        break
                    total_label = f"/{count}" if count else ""
                    log(f"\n--- Lancement ({mode}) {done + 1}{total_label} | Cible réussite: {rate}% ---")
                    if mode == "duel":
                        started = select_and_start_duel(page, log=log)
                    elif mode == "solo":
                        started = select_and_start_solo(page, log=log)
                    else:
                        started = select_and_start_adventure(page, quiz_name, log=log)
                    if not started:
                        break
                    completed = solve_current_quiz(
                        page,
                        model,
                        effort,
                        mode=mode,
                        quiz_name=quiz_name,
                        solo_type=solo_type,
                        success_rate=rate,
                        ephemeral=ephemeral,
                        tracker=tracker,
                        log=log,
                        stop_event=stop_event,
                    )
                    if not completed:
                        break
                    done += 1
                    if count == 0 or done < count:
                        page.wait_for_timeout(2000)

                if mode == "duel":
                    log(f"\n>>> Duels terminés : {done} duel(s) complété(s). <<<")
                elif mode == "solo":
                    log(f"\n>>> Défis solo terminés : {done} défi(s) complété(s). <<<")
                else:
                    log(f"\nSession terminée : {done} partie(s) complétée(s).")
        finally:
            context.close()

    return done


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot autonome AgoraQuiz (Playwright + agy CLI)")
    parser.add_argument("--mode", default="adventure", choices=("adventure", "duel", "solo"), help="Mode de jeu")
    parser.add_argument("--solo-type", default="Premier à 10", help="Type de défi solo")
    parser.add_argument("--url", default="", help="URL directe d'un quiz (optionnel)")
    parser.add_argument("--quiz", default="", help="Nom partiel du thème/quiz à sélectionner")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Modèle agy (défaut: {DEFAULT_MODEL})")
    parser.add_argument("--effort", default=DEFAULT_EFFORT, help="Effort de raisonnement")
    parser.add_argument("--headless", action="store_true", help="Exécuter sans fenêtre visible")
    parser.add_argument("--count", type=int, default=1, help="Nombre de parties à faire (0 = illimité)")
    parser.add_argument(
        "--rate",
        type=int,
        default=DEFAULT_SUCCESS_RATE,
        help=f"Taux de convergence de réussite en %% (0-100, défaut: {DEFAULT_SUCCESS_RATE})",
    )
    parser.add_argument(
        "--keep-history",
        action="store_true",
        help="Conserver les discussions dans agy (sinon supprimées après chaque réponse)",
    )
    args = parser.parse_args()

    app_dir = get_app_dir()
    cfg = load_config(app_dir)
    quiz_target = args.quiz or cfg.get("quiz_name", "")

    run_bot(
        username=cfg["username"],
        password=cfg["password"],
        mode=args.mode,
        model=args.model,
        effort=args.effort,
        headless=args.headless,
        count=args.count,
        quiz_name=quiz_target,
        solo_type=args.solo_type,
        success_rate=args.rate,
        ephemeral=not args.keep_history,
        direct_url=args.url,
        app_dir=app_dir,
    )


if __name__ == "__main__":
    main()
