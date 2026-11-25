from flask import jsonify

LABELS = {
    'FILE_TOO_LARGE':'文件超过 256 KiB', 'FILE_EMPTY':'文件没有内容', 'FILE_BLANK':'文件只有空白',
    'HEADER_ONLY':'文件只有标题行', 'HEADER_MISMATCH':'列名与字段字典不一致', 'ENCODING_INVALID':'请使用 UTF-8 CSV',
    'CSV_MALFORMED':'CSV 引号或行格式异常', 'ROW_LIMIT':'文件超过 2000 行', 'FILE_REQUIRED':'请选择 CSV 文件',
    'RUN_NOT_FOUND':'批次不存在', 'REVIEW_FIELDS':'请填写 3 至 200 字复核意见', 'STALE_REVIEW':'批次已更新，请刷新',
    'RUN_FINALIZED':'批次已签发', 'UNRESOLVED_ISSUES':'存在待处理差异', 'SAMPLE_UNKNOWN':'样例不存在', 'STAGE_UNKNOWN':'处理阶段不存在',
    'APPLICATION_NOT_FOUND':'申请不存在', 'CERTIFICATE_NOT_FOUND':'签发凭证不存在',
    'STALE_POSITION':'申请学分状态已更新，请刷新', 'STALE_CORRECTION':'旧补正不能恢复已撤回课程',
    'WITHDRAWAL_FIELDS_REQUIRED':'请指定撤回课程或配对', 'PAIR_FIELDS_REQUIRED':'请完整提供外校课与校内课配对',
    'INVALID_CREDITS':'学分必须是非负数字', 'INVALID_PRIORITY':'审批等级格式不正确',
    'INVALID_IDENTIFIER':'编号格式不正确', 'INVALID_PAIRS':'配对格式不正确',
    'IDEMPOTENCY_CONFLICT':'相同事件编号的内容不一致',
    'CREDITS_REQUIRED':'请提供培养方案最低学分',
    'UNSUPPORTED_CREDIT_ACTION':'不支持的学分变动类型',
}

NOT_FOUND = {'RUN_NOT_FOUND','SAMPLE_UNKNOWN','STAGE_UNKNOWN','APPLICATION_NOT_FOUND','CERTIFICATE_NOT_FOUND'}
CONFLICT = {'STALE_REVIEW','RUN_FINALIZED','UNRESOLVED_ISSUES','STALE_POSITION','STALE_CORRECTION','IDEMPOTENCY_CONFLICT'}


def response(error):
    code = error.code
    if code in NOT_FOUND or code.endswith('_NOT_FOUND'):
        status = 404
    elif code in CONFLICT or code in {'STALE_REVIEW','RUN_FINALIZED','UNRESOLVED_ISSUES'} or 'CONFLICT' in code or code.startswith('STALE_'):
        status = 409
    elif code == 'FILE_TOO_LARGE':
        status = 413
    else:
        status = 422
    return jsonify(code=code,message=LABELS.get(code,code)),status
