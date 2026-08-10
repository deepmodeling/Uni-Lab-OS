from setuptools import setup, find_packages

package_name = 'unilabos'

setup(
    name=package_name,
    python_requires='>=3.12,<3.13',
    version='0.12.1',
    packages=find_packages(),
    include_package_data=True,
    install_requires=['setuptools'],
    extras_require={
        # Dora 的 Python 包导入名为 ``dora``，并包含 PyArrow；CLI 需单独安装。
        'dora': ['dora-rs'],
    },
    zip_safe=True,
    author="The unilabos developers",
    maintainer='Junhan Chang, Xuwznln',
    maintainer_email='Junhan Chang <changjh@pku.edu.cn>, Xuwznln <18435084+Xuwznln@users.noreply.github.com>',
    description='',
    license='GPL v3',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            "unilab = unilabos.app.main:main"
        ],
    },
)
