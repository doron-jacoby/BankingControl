#!/bin/bash
set -euo pipefail
umask 077

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
finance_root="${FINANCE_INSTALL_ROOT:-$HOME/Library/Application Support/PersonalFinance}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ההתקנה דורשת macOS."
    exit 1
fi

find_python() {
    for candidate in "${FINANCE_PYTHON:-}" /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 python3.14 python3.13 python3.12 python3; do
        if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))' 2>/dev/null; then
            finance_python="$(command -v "$candidate")"
            return 0
        fi
    done
    return 1
}

while ! find_python; do
    echo "שלב הכנה: התקן Python 3.12 ומעלה מהאתר הרשמי: https://www.python.org/downloads/macos/"
    if [[ "${1:-}" == "--check" ]]; then exit 1; fi
    read -r -p "אחרי ההתקנה לחץ Enter לבדיקה חוזרת, או q ליציאה: " reply
    if [[ "$reply" == "q" ]]; then exit 1; fi
done

if [[ "${1:-}" == "--check" ]]; then
    "$finance_python" --version
    echo "בדיקת macOS/Python הצליחה. לא בוצעה התקנה."
    exit 0
fi

echo "האשף יתקין סביבה מבודדת ויפתח התקנה מודרכת בעברית."
echo "הגרסה הנוכחית תומכת בהדגמה בלבד; חיבור לבנק ממתין לתיעוד הספק."
read -r -p "להתקין את חבילות Python בתיקיית PersonalFinance המקומית? [y/N] " reply
case "$reply" in y|Y|yes|כן) ;; *) exit 1 ;; esac

if [[ -L "$finance_root" || -L "$finance_root/runtime" ]]; then
    echo "תיקיית ההתקנה אינה יכולה להיות קישור סימבולי."
    exit 1
fi
mkdir -p "$finance_root"
chmod 700 "$finance_root"
# Stop only this app's existing demo worker before updating its isolated runtime.
/bin/launchctl bootout "gui/$(id -u)/com.personalfinance.demo-worker" >/dev/null 2>&1 || true
"$finance_python" -m venv "$finance_root/runtime"
"$finance_root/runtime/bin/python" -m pip install "$project_root"
exec "$finance_root/runtime/bin/python" -m finance.install --data-dir "$finance_root" "$@"
