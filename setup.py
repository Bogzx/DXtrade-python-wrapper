"""
Setup script for the CTrader Trading SDK package.
"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="ctrader-trading-sdk",
    version="0.1.0",
    author="Your Name",
    author_email="your.email@example.com",
    description="A user-friendly Python SDK for the cTrader Open API v2.0",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/ctrader-trading-sdk",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Financial and Insurance Industry",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Office/Business :: Financial :: Investment",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.8",
    install_requires=[
        "ctrader-open-api>=2.0.0",
    ],
    keywords="ctrader, trading, api, sdk, forex, stocks, finance",
)
