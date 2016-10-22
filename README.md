# httpcopy

监听发往生产服务器的 http 请求，发送给测试服务器，同时记录所有服务器的响应。

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

目录说明：

* forward - 已转发的数据文件
* invalid - 长度为 0 的数据文件，非 http 数据文件等
* invalid_oneway - 单向数据文件
* invalid_server - 不符合服务器地址或文件
* invalid_url - 不符合 url 过滤条件的文件
