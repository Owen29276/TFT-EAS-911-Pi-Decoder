from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="tft-eas-911-pi-decoder",
    version="1.0.0",
    author="Owen",
    description="Emergency Alert System (EAS) receiver and ntfy pusher for Raspberry Pi",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/Owen29276/TFT-EAS-911-Pi-Decoder",
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
        "EAS2Text-Remastered==0.1.25.1",
    ],
    entry_points={
        "console_scripts": [
            "tft-eas-logger=TFT_EAS_911_Pi_logger:main",
            "tft-eas-test=virtual_tft:main",
        ],
    },
)
