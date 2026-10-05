"""拒绝非标准常量和重复字段的纯 JSON 解码。"""

import json


def strict_json_loads(text):
    def invalid_constant(value):
        raise ValueError("JSON 不允许非有限数字常量")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("JSON 存在重复字段")
            result[key] = value
        return result

    return json.loads(text, parse_constant=invalid_constant, object_pairs_hook=unique_pairs)
