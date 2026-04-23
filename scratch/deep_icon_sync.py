import os
import shutil

src_root = r"f:\projects\chromium\src"
db_root = r"f:\projects\ghost_shell_browser\dashboard"

def sync_icons():
    # Mapping for PNGs
    for root, dirs, files in os.walk(os.path.join(src_root, "chrome", "app", "theme")):
        for f in files:
            full_path = os.path.join(root, f)
            
            # Update PNGs
            if f.startswith("product_logo_") and f.endswith(".png"):
                size = f.replace("product_logo_", "").replace(".png", "")
                # Some might have suffixes like _mono or _white
                base_size = "".join(filter(str.isdigit, size))
                if base_size:
                    src_icon = os.path.join(db_root, f"favicon-{base_size}.png")
                    if os.path.exists(src_icon):
                        print(f"Updating PNG: {full_path}")
                        shutil.copy2(src_icon, full_path)
            
            # Update ICOs
            if f in ["chromium.ico", "chromium_doc.ico", "chromium_pdf.ico", "product_logo.ico", "chrome.ico"]:
                src_icon = os.path.join(db_root, "favicon.ico")
                if os.path.exists(src_icon):
                    print(f"Updating ICO: {full_path}")
                    shutil.copy2(src_icon, full_path)

    # Special case: mini_installer
    mini_ico = os.path.join(src_root, "chrome", "installer", "win", "src", "chrome.ico")
    if not os.path.exists(mini_ico):
        mini_ico = os.path.join(src_root, "chrome", "installer", "mini_installer", "mini_installer.ico")
    
    if os.path.exists(mini_ico):
        print(f"Updating Mini Installer ICO: {mini_ico}")
        shutil.copy2(os.path.join(db_root, "favicon.ico"), mini_ico)

if __name__ == "__main__":
    sync_icons()
