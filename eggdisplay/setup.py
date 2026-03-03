from setuptools import setup, find_packages

setup(
    name="rich-text-editor",
    version="0.1.0",
    description="A rich.Live-based text editor component with multi-line editing support",
    author="Egg Assistant",
    packages=find_packages(),
    install_requires=[
        "rich>=10.0.0",
    ],
    python_requires=">=3.7",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
)