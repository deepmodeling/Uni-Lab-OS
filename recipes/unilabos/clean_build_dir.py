import os
import shutil
for item in os.listdir("../.."):
    if item.startswith("."):
        continue
    if item.endswith(".bat"):
        continue
    if item in ("setup.py", "unilabos", "config"):
        continue
    print("****", item)
    if os.path.isfile(item) or os.path.islink(item):
        os.remove(item)
    elif os.path.isdir(item):
        shutil.rmtree(item)
print(os.listdir("../.."))