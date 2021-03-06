'''
Lock and execution script
'''

from __future__ import (absolute_import, division, print_function, unicode_literals)

import atexit
import os
import signal
import socket
import subprocess
import sys
import time
import ConfigParser

import click
import psutil

from kazoo.client import KazooClient, KazooState
from kazoo.exceptions import LockTimeout, ConnectionClosedError
from kazoo.handlers.threading import KazooTimeoutError
from kazoo.security import make_acl
import lockex.glog as log

click.disable_unicode_literals_warning = True


@click.command()
@click.option('--blocking/--no-blocking', help='Block and wait if lock is acquired by another process', default=True)
@click.option('--concurrent', '-c', help='Number of concurrent locks (leases) available, if this is set all clients must have the same value', default=1)
@click.option('--lockid', '-i', help='A string to identify the lock, if none is given an id is generated automatically', default=None)
@click.option('--lockpath', '-p', help='Name of lock path, if no path is given, a lockex is used as a default', default='lockex')
@click.option('--lockretry', '-r', help='How many times to try the command before failing', default=1)
@click.option('--locktimeout', '-t', help='Timeout for waiting for lock aquistion, the default is to wait forever', default=None, type=click.FLOAT)
@click.option('--retry', '-R', help='How many times to try the connecting to zookeeper before failing', default=1)
@click.option('--timeout', '-T', help='Timeout for connecting to zk', default=30)
@click.option('--zkconf', '-C', help='ZooKeeper client config file', default=None, type=click.Path(exists=True, dir_okay=False))
@click.option('--zkhosts', '-z', envvar='ZKHOSTS', help='List of comma seperated zookeeper hosts, in the form of hostname:port', default='localhost:2181')
@click.argument('command', nargs=-1, metavar='<command>')
def execute(blocking, command, concurrent, lockid, lockpath, lockretry, locktimeout, retry, timeout, zkconf, zkhosts,):
    '''
    Main execution logic of getting a lock and executing the user supplied command
    '''
    if command:
        command = " ".join(command).strip()
    else:
        sys.exit(1)

    if lockid is None:
        command_hash = str(abs(hash(command)))
    else:
        command_hash = lockid

    resource = "{0}:{1}".format(socket.gethostname(), os.getpid())
    lockname = "/{0}/{1}".format(lockpath, command_hash)

    command_retry_d = dict(max_tries=lockretry)
    connection_retry_d = dict(max_tries=retry)
    conn, zkhosts = get_zk(zkconf, zkhosts, timeout, command_retry=command_retry_d, connection_retry=connection_retry_d)
    log.info("Locking with zkhosts={zkhosts} lockname={lockname} resource={resource} concurrent={concurrent} blocking={blocking} command='{command}'"
             .format(zkhosts=zkhosts, lockname=lockname, resource=resource, concurrent=concurrent, blocking=blocking, command=command))

    if concurrent > 1:
        lock = conn.Semaphore(lockname, resource, concurrent)
    else:
        lock = conn.Lock(lockname, resource)

    try:
        if concurrent > 1:
            log.info("lease_holders='{0}'".format(",".join(lock.lease_holders())))
        log.info("Want to execute command={command}".format(command=command))
        if lock.acquire(blocking=blocking, timeout=locktimeout):
            log.debug("Executing command={command}")
            job = subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr, shell=True)
            add_signal_helper(job)
            atexit.register(cleanup, job=job, lock=lock, conn=conn)  # don't forget to cleanup at exit
            while job.returncode is None:
                job.poll()
                sys.stdout.flush()
                sys.stderr.flush()
                if job.returncode is None:
                    time.sleep(3)
            cleanup(job=job, lock=lock, conn=conn)  # really kill the processes
            if job:
                sys.exit(job.returncode)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        try:
            atexit.register(cleanup, job=job, lock=lock, conn=conn)
        except UnboundLocalError:
            atexit.register(cleanup, lock=lock, conn=conn)
        sys.exit(1)
    except LockTimeout as exc:
        log.info(exc)
        atexit.register(cleanup, lock=lock, conn=conn)
        sys.exit(1)


def cleanup(conn, lock, job=None):
    ''' Generic cleanup method '''
    if job:
        kill_job(job)
        job.wait()

    try:
        lock.release()
    except ConnectionClosedError:
        pass

    try:
        conn.stop()
    except:
        ''' the connection probably has already been closed '''
        pass

    os.system('stty sane')


def kill_job(job):
    '''
    Kill a subprocess popen job and it's children
    '''
    if job:
        try:
            kill(job.pid)
        except psutil.NoSuchProcess as exc:
            log.error("{0}. May have exited already".format(exc))


def kill(pid):
    '''
    Kill a pid
    '''
    process = psutil.Process(pid)
    try:
        for proc in process.children(recursive=True) + [process]:
            try:
                log.info("Killing pid={0}".format(proc.pid))
                proc.kill()
                time.sleep(0.1)
                proc.terminate()
            except psutil.NoSuchProcess as exc:
                log.error(exc)
    except AttributeError:
        log.info("Killing pid={0}".format(process.pid))
        process.kill()
        time.sleep(0.1)
        try:
            proc.terminate()
        except UnboundLocalError:
            pass


def get_zk(zkconf, zkhosts, timeout, command_retry=None, connection_retry=None):
    '''
    Initiate a zookeeper connection and add a listener
    '''

    def parse_acl(s):
        parts = s.split(':')
        if len(parts) < 3:
            raise RuntimeError("invalid ACL spec: %s" % s)
        scheme, cred, perms = parts[0], parts[1:-1], parts[-1]
        return make_acl(scheme, ':'.join(cred), read='r' in perms, write='w' in perms,
           create='c' in perms, delete='d' in perms, admin='a' in perms, all='*' in perms)

    auth_data = None
    default_acl = None
    sasl_options = None

    if zkconf:
       parser = ConfigParser.ConfigParser()
       with open(zkconf, 'r') as fp:
           parser.readfp(fp, zkconf)
       if zkhosts == 'localhost:2181' and parser.has_option('zookeeper', 'hosts'):
           zkhosts = parser.get('zookeeper', 'hosts').split()
       if parser.has_option('zookeeper', 'auth'):
           auth_data = [tuple(a.split(':', 1)) for a in parser.get('zookeeper', 'auth').split()]
       if parser.has_option('zookeeper', 'default_acl'):
           default_acl = [parse_acl(a) for a in parser.get('zookeeper', 'default_acl').split()]
       if parser.has_section('sasl_auth'):
           sasl_options = dict(parser.items('sasl_auth'))

    conn = KazooClient(hosts=zkhosts, default_acl=default_acl, auth_data=auth_data, sasl_options=sasl_options,
        timeout=timeout, command_retry=command_retry, connection_retry=connection_retry)
    conn.add_listener(listener)
    try:
        conn.start()
    except KazooTimeoutError as exc:
        log.error(exc)
        sys.exit(1)
    return conn, zkhosts


def listener(state):
    '''
    Default listner to log events
    '''
    if state == KazooState.LOST:
        log.error(state)
        os.kill(os.getpid(), signal.SIGTERM)
        sys.exit(1)
    elif state == KazooState.SUSPENDED:
        log.error(state)
    else:
        pass


def add_signal_helper(process):
    '''
    Signal helper, accepts a psutil process object
    '''
    def handle_sig(sig, frame):
        ''' Kill and exit '''
        kill(process.pid)
        process.wait()
        os.system('stty sane')
        sys.exit(process.returncode)

    def reap(sig, frame):
        ''' do nothing '''
        pass

    signal.signal(signal.SIGTERM, handle_sig)
    signal.signal(signal.SIGHUP, reap)
    signal.signal(signal.SIGINT, reap)
    signal.signal(signal.SIGUSR1, reap)
    signal.signal(signal.SIGUSR2, reap)
    signal.signal(signal.SIGQUIT, reap)
    signal.signal(signal.SIGCHLD, reap)


if __name__ == '__main__':
    execute()  # pylint: disable=no-value-for-parameter
