"""保守检查历史进程组是否仍存在；不杀进程、不匹配命令行。"""

import os


def process_is_alive(record):
    pid = record.get("pid")
    group = record.get("process_group")
    if type(pid) is not int or pid <= 0:
        # 缺失/非法归属证明不能推断进程已经结束。
        return True
    try:
        if os.name == "posix":
            if type(group) is not int or group != pid:
                return True
            os.killpg(group, 0)
        else:
            # 非 POSIX 不用 kill(pid, 0) 推测身份；保守阻止接管。
            return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
