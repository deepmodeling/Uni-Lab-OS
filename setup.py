from setuptools import setup, find_packages

package_name = 'unilabos'

setup(
    name=package_name,
    version='0.10.14',
    packages=find_packages(),
    include_package_data=True,
    install_requires=['setuptools'],
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
