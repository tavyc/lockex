from setuptools import setup, find_packages
setup(name="lockex",
      version="0.3",
      description="Get lock from zookeeper and execute",
      packages=find_packages(exclude=["__pycache__"]),
      install_requires=['click==7.1.1', 'python_gflags==3.1.2', 'kazoo==2.8.0', 'pure-sasl==0.6.2', 'psutil==5.7.0', 'future==0.18.2'],
      setup_requires=['flake8==2.5.4'],
      tests_require=['tox==2.3.1', 'pytest==2.6.3', 'testfixtures==4.9.1', 'mock==1.0.1'],
      entry_points={'console_scripts': ['lockex = lockex.execute:execute']},
      extras_require=dict(test=['testfixtures'],),
      license='BSD',)
