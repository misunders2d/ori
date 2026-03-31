import re

def patch_sh(filepath):
    with open(filepath, 'r') as f:
        content = f.read()
    
    extraction = """
BOT_NAME="Ori"
if [ -f "data/.env" ]; then
  ENV_BOT_NAME=$(grep -v '^#' data/.env | grep -E '^BOT_NAME=' | cut -d '=' -f2- | tr -d '\\"\\'\\r' | xargs 2>/dev/null)
  if [ ! -z "$ENV_BOT_NAME" ]; then BOT_NAME="$ENV_BOT_NAME"; fi
fi
"""
    content = content.replace("#!/bin/bash\n", "#!/bin/bash\n" + extraction)
    content = content.replace("[Ori]", "[$BOT_NAME]")
    content = content.replace("Ori:", "$BOT_NAME:")
    
    with open(filepath, 'w') as f:
        f.write(content)

def patch_bat(filepath):
    with open(filepath, 'r') as f:
        content = f.read()

    extraction = """
set BOT_NAME=Ori
if exist "data\\.env" (
    for /f "tokens=1,* delims==" %%A in ('type "data\\.env" ^| findstr "^BOT_NAME="') do set BOT_NAME=%%B
)
set BOT_NAME=%BOT_NAME:"=%
"""
    if "@echo off\n" in content:
        content = content.replace("@echo off\n", "@echo off\n" + extraction)
    else:
        content = extraction.lstrip() + content

    content = content.replace("[Ori]", "[%BOT_NAME%]")
    content = content.replace("Ori:", "%BOT_NAME%:")
    
    with open(filepath, 'w') as f:
        f.write(content)

patch_sh('rollback.sh')
patch_bat('rollback.bat')
