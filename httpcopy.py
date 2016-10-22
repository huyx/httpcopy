#!/usr/bin/env python3

"""监听发往生产服务器的 http 请求，发送给测试服务器，同时记录所有服务器的响应。

假设环境如下：

* 生产服务器(eth1)： 192.168.1.132:80
* 测试服务器： 192.168.1.104:80

httpcopy 配合 tcpflow 工作，方法是：

* 启用 tcpflow 监听生产服务器：`tcpflow -i eth1 host 192.168.1.132 and port 80`
* 运行 httpcopy：`httpcopy -l 192.168.1.132:80 -f 192.168.1.104:80`

httpcopy 转发数据的流程：

* tcpflow 把监听到的数据保存到 http-data
* httpcopy 监视 http-data 中的数据文件，如果文件长时间不再发生变化时，认为本次请求已经结束
* httpcopy 转发 http 请求数据

问题：

* 考虑到 Keep-alive 特性，要考虑超时时间和文件覆盖的问题
* httpcopy 只进行数据转发，没有进行 http 协议的分析，无法完全模拟 http 的一发一收的过程
"""
import argparse
import glob
import os
import re
import socket
import threading
import time
import logging

DEFAULT_URL_PREFIX = ''

FORWARD_CONN_TIMEOUT = 2    # 转发连接超时
FORWARD_DATA_TIMEOUT = 5    # 转发数据超时

RE_HTTP_REQ = re.compile('([A-Z]{3,8}) ([^ ]+) (HTTP/1\.[01])')
RE_HTTP_RESP = re.compile('(HTTP/1.[01]) (\d{3}) (.*)')

config = argparse.Namespace()
logger = logging.Logger


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-l', '--listen',
                        metavar='SVR',
                        help='要监听的服务器.')
    parser.add_argument('-f', '--forward',
                        metavar='SVR',
                        help='要转发的服务器.')
    parser.add_argument('-u', '--url-prefix',
                        metavar='PATH',
                        default=DEFAULT_URL_PREFIX,
                        help='URL 前缀，用于过滤 HTTP 请求.'
                        )
    parser.add_argument('-t', '--timeout',
                        metavar='N',
                        type=int,
                        default=10,
                        help='检测文件变化的超时时间.')
    parser.add_argument('-i', '--interval',
                        metavar='N',
                        type=float,
                        default=1,
                        help='检测间隔(0 表示运行一次后退出).')
    parser.add_argument('-d', '--data-dir',
                        metavar='DIR',
                        default='.',
                        help='数据目录.')
    return parser.parse_args()


def parse_hostport(hostport, default_port=80):
    if ':' in hostport:
        host, port = hostport.split(':')
        port = int(port)
    else:
        host = hostport
        port = default_port
    return host, port


def format_hostport(host, port):
    f1, f2, f3, f4 = map(int, host.split('.'))
    return '{:03d}.{:03d}.{:03d}.{:03d}.{:05d}'.format(f1, f2, f3, f4,port)


def read_firstline(fn):
    return open(fn, encoding='iso=8859-1').readline(2048)


def check_http_files(ready_files):
    invalid_files = []
    http_files = []
    invalid_url_files = []  # 过滤掉的文件
    for fn, peer_fn in ready_files:
        line = read_firstline(fn)
        peer_line = read_firstline(peer_fn)

        # 检查是否空文件
        if not line or not peer_line:
            invalid_files.append(fn)
            invalid_files.append(peer_fn)
            continue

        # 检查哪个文件是 http 请求，哪个文件是 http 响应
        if RE_HTTP_REQ.match(line) and RE_HTTP_RESP.match(peer_line):
            request_line = line
            request_file = fn
            response_file = peer_fn
        elif RE_HTTP_REQ.match(peer_line) and RE_HTTP_RESP.match(line):
            request_line = peer_line
            request_file = peer_fn
            response_file = fn
        else:
            invalid_files.append(fn)
            invalid_files.append(peer_fn)
            continue

        url = request_line.split(' ')[1]
        if config.url_prefix and not url.startswith(config.url_prefix):
            invalid_url_files.append(fn)
            invalid_url_files.append(peer_fn)
        else:
            logger.info('转发: %s', request_line.rstrip())
            http_files.append((request_file, response_file))

    move_files(invalid_url_files, config.data_dir_invalid_url)

    return http_files, invalid_files


def move_files(invalid_files, data_dir):
    for fn in invalid_files:
        os.rename(fn, os.path.join(data_dir, fn))


def forward(request_file, response_file):
    with socket.socket() as sock:
        sock.settimeout(FORWARD_CONN_TIMEOUT)
        try:
            sock.connect(config.forward)
        except socket.timeout:
            logger.info('连接超时: %s', config.forward)
            return
        except ConnectionRefusedError as e:
            logger.info('无法连接: %s, %s', config.forward, e)
            return
        for line in open(request_file, 'rb'):
            sock.send(line)
        sock.settimeout(FORWARD_DATA_TIMEOUT)
        while True:
            try:
                data = sock.recv(1024)
            except socket.timeout:
                break
            if not data:
                break
            with open(response_file, 'ab') as fp:
                fp.write(data)


def process_http_files(request_file, response_file):
    prefix = time.strftime('%Y%m%d_%H%M%S_')

    new_request_file = os.path.join(config.data_dir_forward, prefix + request_file + '-request')
    new_response_file = os.path.join(config.data_dir_forward, prefix + response_file + '-response')
    forward_response_file = os.path.join(config.data_dir_forward, prefix + response_file + '-forward-response')

    os.rename(request_file, new_request_file)
    os.rename(response_file, new_response_file)

    t = threading.Thread(target=forward, args=(new_request_file, forward_response_file))
    t.setDaemon(True)
    t.start()


def process():
    now = time.time()
    timeout = config.timeout
    listen_server = format_hostport(*config.listen)

    # 所有符合 tcpflow 输出格式的文件名
    all_files = glob.glob('???.???.???.???.?????-???.???.???.???.?????')
    valid_files = [ fn for fn in all_files
                    if fn.startswith(listen_server) or fn.endswith(listen_server) ]

    # 处理无效文件
    invalid_server_files = set(all_files) - set(valid_files)
    move_files(invalid_server_files, config.data_dir_invalid_server)

    # 检查文件修改时间
    mtimes = { fn: os.stat(fn).st_mtime for fn in valid_files }
    timeout_files = [ fn for fn in valid_files
                      if now - mtimes[fn] > timeout ]

    # 根据数据流方向过滤文件
    ready_files = []
    invalid_oneway_files = []
    while timeout_files:
        fn = timeout_files.pop(0)
        peer_fn = '{1}-{0}'.format(*fn.split('-'))
        if peer_fn in timeout_files:
            timeout_files.remove(peer_fn)
            ready_files.append((fn, peer_fn))
        elif peer_fn in all_files:
            # 另一个方向还有数据，暂不处理
            pass
        else:
            invalid_oneway_files.append(fn)

    move_files(invalid_oneway_files, config.data_dir_invalid_oneway)

    # 检查是否 http 请求
    http_files, invalid_files = check_http_files(ready_files)

    move_files(invalid_files, config.data_dir_invalid)

    # 处理 http 文件
    for request_file, response_file in http_files:
        process_http_files(request_file, response_file)


def httpcopy():
    config.listen = parse_hostport(config.listen)
    config.forward = parse_hostport(config.forward)

    config.data_dir_forward = os.path.join(config.data_dir, 'forward')
    config.data_dir_invalid = os.path.join(config.data_dir, 'invalid')
    config.data_dir_invalid_oneway = os.path.join(config.data_dir, 'invalid_oneway')
    config.data_dir_invalid_server = os.path.join(config.data_dir, 'invalid_server')
    config.data_dir_invalid_url = os.path.join(config.data_dir, 'invalid_url')

    os.makedirs(config.data_dir_forward, exist_ok=True)
    os.makedirs(config.data_dir_invalid, exist_ok=True)
    os.makedirs(config.data_dir_invalid_oneway, exist_ok=True)
    os.makedirs(config.data_dir_invalid_server, exist_ok=True)
    os.makedirs(config.data_dir_invalid_url, exist_ok=True)

    while True:
        process()
        if config.interval:
            time.sleep(config.interval)
        else:
            break


if __name__ == "__main__":
    config = parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s - %(levelname)s - %(message)s')
    logger = logging.getLogger('httpcopy')

    logger.info('开始.')
    try:
        httpcopy()
    except KeyboardInterrupt:
        pass
    logger.info('退出.')
