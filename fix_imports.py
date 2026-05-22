import os

for root, dirs, files in os.walk("."):
    if ".venv" in dirs:
        dirs.remove(".venv")
    for file in files:
        if file.endswith(".py") and file != "fix_imports.py":
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            new_lines = []
            has_typing = False
            typing_idx = -1
            future_idx = -1
            
            for i, line in enumerate(lines):
                if line.startswith("from typing import"):
                    has_typing = True
                    typing_idx = i
                    # Inject Optional, Union, Any
                    for t in ["Optional,", "Union,", "Any,"]:
                        if t.strip(",") not in line:
                            line = line.replace("import ", f"import {t} ")
                elif line.startswith("from __future__ import annotations"):
                    future_idx = i
                new_lines.append(line)
            
            if not has_typing and future_idx != -1:
                # Insert just after from __future__
                new_lines.insert(future_idx + 1, "from typing import Optional, Union, Any\n")
            
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"Fixed imports in {path}")
