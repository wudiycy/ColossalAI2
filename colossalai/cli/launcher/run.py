import click
import sys
import os
import torch
from colossalai.context import Config
from .multinode_runner import MultiNodeRunner
from .hostinfo import HostInfo, HostInfoList
from typing import List
from packaging import version

# Constants that define our syntax
NODE_SEP = ','


def fetch_hostfile(hostfile_path: str, ssh_port: int) -> HostInfoList:
    """
    Parse the hostfile to obtain a list of hosts.
    
    A hostfile should look like:
    worker-0
    worker-1
    worker-2
    ...

    Args:
        hostfile_path (str): the path to the hostfile
        ssh_port (int): the port to connect to the host
    """

    if not os.path.isfile(hostfile_path):
        click.echo(f"Error: Unable to find the hostfile, no such file: {hostfile_path}")
        exit()

    with open(hostfile_path, 'r') as fd:
        device_pool = HostInfoList()

        for line in fd.readlines():
            line = line.strip()
            if line == '':
                # skip empty lines
                continue

            # build the HostInfo object
            hostname = line.strip()
            hostinfo = HostInfo(hostname=hostname, port=ssh_port)

            if device_pool.has(hostname):
                click.echo(f"Error: found duplicate host {hostname} in the hostfile")
                exit()

            device_pool.append(hostinfo)
    return device_pool


def parse_device_filter(device_pool: HostInfoList, include_str=None, exclude_str=None) -> HostInfoList:
    '''Parse an inclusion or exclusion string and filter a hostfile dictionary.

    Examples:
        include_str="worker-0,worker-1" will execute jobs only on worker-0 and worker-1.
        exclude_str="worker-1" will use all available devices except worker-1.

    Args:
        device_pool (HostInfoList): a list of HostInfo objects
        include_str (str): --include option passed by user, default None
        exclude_str (str): --exclude option passed by user, default None
    
    Returns:
        filtered_hosts (HostInfoList): filtered hosts after inclusion/exclusion
    '''

    # Ensure include/exclude are mutually exclusive
    if include_str and exclude_str:
        click.echo("--include and --exclude are mutually exclusive, only one can be used")
        exit()

    # no-op
    if include_str is None and exclude_str is None:
        return device_pool

    # Either build from scratch or remove items
    if include_str:
        parse_str = include_str
        filtered_hosts = HostInfoList()
    elif exclude_str:
        parse_str = exclude_str
        filtered_hosts = device_pool

    # foreach node in the list
    for node_config in parse_str.split(NODE_SEP):
        hostname = node_config
        hostinfo = device_pool.get_hostinfo(hostname)
        # sanity check hostname
        if not device_pool.has(hostname):
            click.echo(f"Error: Hostname '{hostname}' not found in hostfile")
            exit()

        if include_str:
            filtered_hosts.append(hostinfo)
        elif exclude_str:
            filtered_hosts.remove(hostname)

    return filtered_hosts


def get_launch_command(
    master_addr: str,
    master_port: int,
    nproc_per_node: int,
    user_script: str,
    user_args: List[str],
    node_rank: int,
    num_nodes: int,
    extra_launch_args: str = None,
) -> str:
    """
    Generate a command for distributed training.

    Args:
        master_addr (str): the host of the master node
        master_port (str): the port of the master node
        nproc_per_node (str): the number of processes to launch on each node
        user_script (str): the user Python file
        user_args (str): the arguments for the user script
        node_rank (int): the unique ID for the node
        num_nodes (int): the number of nodes to execute jobs

    Returns:
        cmd (str): the command the start distributed training
    """

    def _arg_dict_to_list(arg_dict):
        ret = []

        for k, v in arg_dict.items():
            if v:
                ret.append(f'--{k}={v}')
            else:
                ret.append(f'--{k}')
        return ret

    if extra_launch_args:
        extra_launch_args_dict = dict()
        for arg in extra_launch_args.split(','):
            if '=' in arg:
                k, v = arg.split('=')
                extra_launch_args_dict[k] = v
            else:
                extra_launch_args_dict[arg] = None
        extra_launch_args = extra_launch_args_dict
    else:
        extra_launch_args = dict()

    torch_version = version.parse(torch.__version__)
    assert torch_version.major == 1

    if torch_version.minor < 9:
        cmd = [
            sys.executable, "-m", "torch.distributed.launch", f"--nproc_per_node={nproc_per_node}",
            f"--master_addr={master_addr}", f"--master_port={master_port}", f"--nnodes={num_nodes}",
            f"--node_rank={node_rank}"
        ]
    else:
        # extra launch args for torch distributed launcher with torch >= 1.9
        default_torchrun_rdzv_args = dict(rdzv_backend="c10d",
                                          rdzv_endpoint=f"{master_addr}:{master_port}",
                                          rdzv_id="colossalai-default-job")

        # update rdzv arguments
        for key in default_torchrun_rdzv_args.keys():
            if key in extra_launch_args:
                value = extra_launch_args.pop(key)
                default_torchrun_rdzv_args[key] = value

        if torch_version.minor < 10:
            cmd = [
                sys.executable, "-m", "torch.distributed.run", f"--nproc_per_node={nproc_per_node}",
                f"--nnodes={num_nodes}", f"--node_rank={node_rank}"
            ]
        else:
            cmd = [
                "torchrun", f"--nproc_per_node={nproc_per_node}", f"--nnodes={num_nodes}", f"--node_rank={node_rank}"
            ]
        cmd += _arg_dict_to_list(default_torchrun_rdzv_args)

    cmd += _arg_dict_to_list(extra_launch_args) + [user_script] + user_args
    cmd = ' '.join(cmd)
    return cmd


def launch_multi_processes(args: Config) -> None:
    """
    Launch multiple processes on a single node or multiple nodes.

    The overall logic can be summarized as the pseudo code below:
    
        if hostfile given:
            hostinfo = parse_hostfile(hostfile)
            hostinfo = include_or_exclude_hosts(hostinfo)
            launch_on_multi_nodes(hostinfo)
        elif hosts given:
            hostinfo = parse_hosts(hosts)
            launch_on_multi_nodes(hostinfo)
        else:
            launch_on_current_node()
    
    Args:
        args (Config): the arguments taken from command line

    """
    assert isinstance(args, Config)

    if args.nproc_per_node < 0:
        click.echo("--nproc_per_node received an invalid value which is smaller than 1")

    # cannot accept hosts and hostfile at the same time
    if args.host and args.hostfile:
        click.echo("Error: hostfile and hosts are mutually exclusive, only one is required")

    # check if hostfile is given
    if args.hostfile:
        device_pool = fetch_hostfile(args.hostfile, ssh_port=args.ssh_port)
        active_device_pool = parse_device_filter(device_pool, args.include, args.exclude)

        if args.num_nodes > 0:
            # only keep the first num_nodes to execute jobs
            updated_active_device_pool = HostInfoList()
            for count, hostinfo in enumerate(active_device_pool):
                if args.num_nodes == count:
                    break
                updated_active_device_pool.append(hostinfo)
            active_device_pool = updated_active_device_pool
    else:
        active_device_pool = None

    env = os.environ.copy()

    # use hosts if hostfile is not given
    if args.host and active_device_pool is None:
        active_device_pool = HostInfoList()
        host_list = args.host.strip().split(NODE_SEP)
        for hostname in host_list:
            hostinfo = HostInfo(hostname=hostname, port=args.ssh_port)
            active_device_pool.append(hostinfo)

    if not active_device_pool:
        # run on local node if not hosts or hostfile is given
        # add local node to host info list
        active_device_pool = HostInfoList()
        localhost_info = HostInfo(hostname='127.0.0.1', port=args.ssh_port)
        active_device_pool.append(localhost_info)

    # launch distributed processes
    runner = MultiNodeRunner()
    curr_path = os.path.abspath('.')

    # collect current path env
    env = dict()
    for k, v in os.environ.items():
        # do not support multi-line env var
        if v and '\n' not in v:
            env[k] = v

    # establish remote connection
    runner.connect(host_info_list=active_device_pool, workdir=curr_path, env=env)

    # execute distributed launching command
    for node_id, hostinfo in enumerate(active_device_pool):
        cmd = get_launch_command(master_addr=args.master_addr,
                                 master_port=args.master_port,
                                 nproc_per_node=args.nproc_per_node,
                                 user_script=args.user_script,
                                 user_args=args.user_args,
                                 node_rank=node_id,
                                 num_nodes=len(active_device_pool),
                                 extra_launch_args=args.extra_launch_args)
        runner.send(hostinfo=hostinfo, cmd=cmd)

    runner.recv_from_all()
    runner.stop_all()
    runner.recv_from_all()
