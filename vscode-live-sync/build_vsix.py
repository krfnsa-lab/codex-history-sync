"""Build a local VSIX without downloading build dependencies."""
import argparse
from pathlib import Path
import json
import zipfile
from xml.sax.saxutils import escape
from build_windows_launcher import build

ROOT = Path(__file__).resolve().parent

def package(output):
    extension = ROOT / 'vscode-extension'
    meta = json.loads((extension / 'package.json').read_text())
    launcher = extension / 'bin/windows/codex-history-sync-bridge.exe'
    build(launcher)
    manifest = f'''<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata><Identity Language="en-US" Id="{escape(meta['name'])}" Version="{meta['version']}" Publisher="{meta['publisher']}"/>
<DisplayName>{escape(meta['displayName'])}</DisplayName><Description xml:space="preserve">{escape(meta['description'])}</Description>
<Tags>codex,wsl</Tags><Categories>Other</Categories><GalleryFlags>Preview</GalleryFlags>
<Properties><Property Id="Microsoft.VisualStudio.Code.Engine" Value="{escape(meta['engines']['vscode'])}"/>
<Property Id="Microsoft.VisualStudio.Code.ExtensionKind" Value="workspace"/></Properties></Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/>
<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
<Asset Type="Microsoft.VisualStudio.Services.Content.Details" Path="extension/README.md" Addressable="true"/>
<Asset Type="Microsoft.VisualStudio.Services.Content.License" Path="extension/LICENSE" Addressable="true"/></Assets></PackageManifest>'''
    content_types = '''<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="vsixmanifest" ContentType="text/xml"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="py" ContentType="text/plain"/><Default Extension="md" ContentType="text/markdown"/><Default Extension="exe" ContentType="application/octet-stream"/><Default Extension="" ContentType="application/octet-stream"/></Types>'''
    files = {name: extension / name for name in ['package.json', 'extension.js', 'bridge.js', 'README.md']}
    files.update({'gateway.py': ROOT / 'gateway.py', 'LICENSE': ROOT / 'LICENSE',
                  'DISTLIB-LICENSE.txt': ROOT / 'DISTLIB-LICENSE.txt', 'THIRD_PARTY_NOTICES.md': ROOT / 'THIRD_PARTY_NOTICES.md',
                  'WINDOWS-LAUNCHER-LICENSE.txt': ROOT / 'WINDOWS-LAUNCHER-LICENSE.txt',
                  'bin/windows/codex-history-sync-bridge.exe': launcher,
                  'bin/linux/codex-history-sync-bridge': ROOT / 'linux_launcher.py'})
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('extension.vsixmanifest', manifest)
        archive.writestr('[Content_Types].xml', content_types)
        for name, source in files.items():
            info = zipfile.ZipInfo('extension/' + name)
            info.external_attr = (0o100755 if name.startswith('bin/linux/') else 0o100644) << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return output

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/codex-history-live-sync-0.1.2.vsix')
    print(package(parser.parse_args().output))
