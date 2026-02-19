from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="tft911-eas",
    version="1.0.0",
    author="Owen Schnell",
    description="Emergency Alert System (EAS) receiver for Raspberry Pi",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/owenschnell/tft911-eas",
    packages=find_packages(),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "Topic :: Communications",
        "Topic :: System :: Monitoring",
    ],
    python_requires=">=3.10",
    install_requires=[
        "pyserial>=3.5",
        "requests>=2.31.0",
        "EAS2Text-Remastered>=1.0.0",
    ],
    entry_points={
        "console_scripts": [
            "tft911-logger=TFT_EAS_911_Pi_logger:main",
            "tft911-test=virtual_tft:main",
        ],
    },
)
