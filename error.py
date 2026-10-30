def find_max(numbers):
    if numbers is None or not numbers:
        return "没有数据"

    max_num = numbers[0]

    for num in numbers:
        if num > max_num:
            max_num = num
        else:
            print("不需要更新最大值")

    return max_num


numbers = [3, 8, 2, 10, 5]
result = find_max(numbers)
print("最大值是：", result)
