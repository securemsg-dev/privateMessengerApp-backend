import os
import re

for root, dirs, files in os.walk("."):
    if ".venv" in dirs:
        dirs.remove(".venv")
    for file in files:
        if file.endswith(".py") and file != "fix_types.py":
            path = os.path.join(root, file)
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            new_content = content
            
            # str | UUID | None -> Optional[Union[str, UUID]]
            new_content = re.sub(r'str\s*\|\s*UUID\s*\|\s*None', 'Optional[Union[str, UUID]]', new_content)
            
            # str | UUID -> Union[str, UUID]
            new_content = re.sub(r'str\s*\|\s*UUID', 'Union[str, UUID]', new_content)
            
            # Mapped[Something | None] -> Mapped[Optional[Something]]
            # This regex allows quotes and brackets inside the Mapped block
            new_content = re.sub(r'Mapped\[([A-Za-z0-9_\[\]" \.]*?)\s*\|\s*None\]', r'Mapped[Optional[\1]]', new_content)
            
            # Bare types: Something | None -> Optional[Something]
            new_content = re.sub(r':\s*([A-Za-z0-9_\[\]" \.]*?)\s*\|\s*None', r': Optional[\1]', new_content)
            new_content = re.sub(r'->\s*([A-Za-z0-9_\[\]" \.]*?)\s*\|\s*None', r'-> Optional[\1]', new_content)

            # Extra specific cases for Python 3.9
            new_content = new_content.replace('dict[str, Any] | None', 'Optional[dict[str, Any]]')

            # Add Optional / Union imports if changed
            if new_content != content:
                imports_to_add = []
                if "Optional[" in new_content and "Optional" not in content:
                    imports_to_add.append("Optional")
                if "Union[" in new_content and "Union" not in content:
                    imports_to_add.append("Union")

                # Simply ensure they are imported from typing
                if imports_to_add:
                    if "from typing import" in new_content:
                        new_content = new_content.replace('from typing import Any, Union, Optional, ', f'from typing import {", ".join(imports_to_add)}, ')
                    elif "from __future__ import annotations\n" in new_content:
                        new_content = new_content.replace(
                            'from __future__ import annotations\n',
                            f'from __future__ import Any, Union, Optional, annotations\nfrom typing import {", ".join(imports_to_add)}\n'
                        )

            if new_content != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"Updated {path}")
