import pathlib
import shutil
import subprocess
import sys

import setuptools
import setuptools.command.build_ext
import setuptools.extension

dirname = pathlib.Path(__file__).resolve().parent

with open(dirname / "README.md") as file:
    long_description = file.read()

if not "-h" in sys.argv and not "--help" in sys.argv:
    manifest_lines = []
    if "sdist" in sys.argv:
        manifest_lines.append(f"include configuration-schema.json")
    else:
        shutil.rmtree(dirname / "charidotella" / "assets", ignore_errors=True)
        (dirname / "charidotella" / "assets").mkdir()
        shutil.copy2(
            dirname / "configuration-schema.json",
            dirname / "charidotella" / "assets" / "configuration-schema.json",
        )
        manifest_lines.append(f"include charidotella/assets/configuration-schema.json")
    with open("MANIFEST.in", "w") as manifest:
        content = "\n".join(manifest_lines)
        manifest.write(f"{content}\n")

exec(open(dirname / "charidotella" / "version.py").read())

setuptools.setup(
    name="charidotella",
    version=__version__,  # type: ignore
    url="https://github.com/neuromorphicsystems/charidotella",
    author="Alexandre Marcireau",
    author_email="alexandre.marcireau@gmail.com",
    description="Charidotella is a toolbox to organise and visualise Event Stream (.es) recordings",
    long_description=long_description,
    long_description_content_type="text/markdown",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
    ],
    packages=[
        "charidotella",
        "charidotella.filters",
        "charidotella.tasks",
        "charidotella.assets",
    ],
    include_package_data=True,
    package_data={"": ["charidotella/assets/*"]},
    install_requires=[
        "aedat",
        "colourtime",
        "coolname",
        "event_stream",
        "jsonschema",
        "matplotlib",
        "pillow>=9.1",
        "scipy",
        "toml",
    ],
    entry_points={
        "console_scripts": [
            "charidotella = charidotella:main",
        ]
    },
)
