import base64
import re

import pymysql
from flask import current_app

from .models import Dbconfig


def fetch_all(sql_content, host, port, user, password, db_in):
    """
    封装mysql连接和获取结果集方法
    :param sql_content:
    :param host:
    :param port:
    :param user:
    :param password:
    :param db_in:
    :return:
    """
    result = None
    conn = None
    cur = None
    sql_content = sql_content.encode('utf-8').decode('utf-8')

    try:
        conn = pymysql.connect(
            host=host,
            user=user,
            password=password,
            db=db_in,
            port=port,
            charset='utf8mb4'
        )
        cur = conn.cursor()
        cur.execute(sql_content)
        result = cur.fetchall()
    except pymysql.InternalError as e:
        print("Mysql Error %d: %s" % (e.args[0], e.args[1]))
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

    return result


def critical_ddl(sql_content):
    """
    识别DROP DATABASE, DROP TABLE, TRUNCATE PARTITION, TRUNCATE TABLE等高危DDL操作，因为对于这些操作，inception在备份时只能备份METADATA，而不会备份数据！
    如果识别到包含高危操作，则返回“审核不通过”
    """
    result_list = []
    critical_sql_found = 0
    for row in sql_content.rstrip(';').split(';'):
        if re.match(
                r"([\s\S]*)drop(\s+)database(\s+.*)|([\s\S]*)drop(\s+)table(\s+.*)|([\s\S]*)truncate(\s+)partition(\s+.*)|([\s\S]*)truncate(\s+)table(\s+.*)",
                row.lower()
        ):
            result = (
                '',
                '',
                2,
                '驳回高危SQL',
                '不能包含【DROP DATABASE】|【DROP TABLE】|【TRUNCATE PARTITION】|【TRUNCATE TABLE】关键字！',
                row,
                '',
                '',
                '',
                ''
            )
            critical_sql_found = 1
        else:
            result = ('', '', 0, '', 'None', row, '', '', '', '')
        result_list.append(result)

    if critical_sql_found == 1:
        return result_list
    else:
        return None


def pre_check(sql_content):
    """
    在提交给inception之前，预先识别一些Inception不能正确审核的SQL,比如"alter table t1;"或"alter table test.t1;" 以免导致inception core dump
    :param sql_content:
    :return:
    """
    result_list = []
    syntax_error_sql_found = 0
    for row in sql_content.rstrip(';').split(';'):
        if re.match(
                r"(\s*)alter(\s+)table(\s+)(\S+)(\s*);|(\s*)alter(\s+)table(\s+)(\S+)\.(\S+)(\s*);",
                row.lower() + ";"
        ):
            result = ('', '', 2, 'SQL语法错误', 'ALTER must have options', row, '', '', '', '')
            syntax_error_sql_found = 1
        else:
            result = ('', '', 0, '', 'None', row, '', '', '', '')

        result_list.append(result)

    if syntax_error_sql_found == 1:
        return result_list
    else:
        return None


def sql_auto_review(sql_content, db_in_name, is_split="no"):
    """
    SQL Auto Review via Inception
    """
    db_in = Dbconfig.query.filter(Dbconfig.name == db_in_name).first()
    db_host = db_in.master_host
    db_port = db_in.master_port
    db_user = db_in.username
    db_password = base64.b64decode(db_in.password.encode('utf-8'))
    db_password = db_password.decode('utf-8')

    critical_ddl_config = current_app.config['CRITICAL_DDL_ON_OFF']
    if critical_ddl_config == "ON":
        critical_ddl_check = critical_ddl(sql_content)
    else:
        critical_ddl_check = None

    if critical_ddl_check is not None:
        result = critical_ddl_check
    else:
        pre_check_result = pre_check(sql_content)
        if pre_check_result is not None:
            result = pre_check_result
        else:
            if is_split == 'yes':
                # 这种场景只给osc进度功能使用
                # 如果一个工单中同时包含DML和DDL，那么执行时被split后的SQL与提交的SQL会不一样（会在每条语句前面加use database;)，导致osc进度更新取不到正确的SHA1值。
                # 请参考inception文档中--enable-split参数的说明
                sql_split = "/*--user=%s; --password=%s; --host=%s; --enable-execute;--port=%s; --enable-ignore-warnings;--enable-split;*/\
                             inception_magic_start;\
                             %s\
                             inception_magic_commit;" % (db_user, db_password, db_host, str(db_port), sql_content)
                split_result = fetch_all(sql_split, current_app.config['INCEPTION_HOST'],
                                         current_app.config['INCEPTION_PORT'], '', '', '')
                tmp_list = []
                for split_row in split_result:
                    sql_tmp = split_row[1]
                    sql = "/*--user=%s;--password=%s;--host=%s;--enable-check;--port=%s; --enable-ignore-warnings;*/\
                            inception_magic_start;\
                            %s\
                            inception_magic_commit;" % (db_user, db_password, db_host, str(db_port), sql_tmp)
                    review_result = fetch_all(sql, current_app.config['INCEPTION_HOST'],
                                              current_app.config['INCEPTION_PORT'], '', '', '')
                    tmp_list.append(review_result)

                # 二次加工下
                final_list = []
                for split_row in tmp_list:
                    for sql_row in split_row:
                        final_list.append(list(sql_row))
                result = final_list
            else:
                # 工单审核使用
                sql = "/*--user=%s;--password=%s;--host=%s;--enable-check=1;--port=%s;*/\
                        inception_magic_start;\
                        %s\
                        inception_magic_commit;" % (db_user, db_password, db_host, str(db_port), sql_content)
                result = fetch_all(sql, current_app.config['INCEPTION_HOST'], current_app.config['INCEPTION_PORT'], '',
                                   '', '')

    return result
