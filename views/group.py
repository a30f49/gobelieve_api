# -*- coding: utf-8 -*-
import config
from flask import request, Blueprint
from flask import g
import logging
import pymysql
import json
import time
from authorization import require_application_auth
from models.group_model import Group
from models.user import User

from libs.util import make_response
from libs.response_meta import ResponseMeta
from rpc import send_group_notification

app = Blueprint('group', __name__)

publish_message = Group.publish_message
        
@app.route("/groups", methods=["POST"])
@require_application_auth
def create_group():
    appid = request.appid
    obj = json.loads(request.data)
    master = obj["master"]
    name = obj["name"]
    is_super = obj["super"] if obj.has_key("super") else False
    members = obj["members"]

    if hasattr(request, 'uid') and request.uid != master:
        raise ResponseMeta(400, "master must be self")
        
    gid = 0
    if config.EXTERNAL_GROUP_ID:
        gid = obj['group_id'] if obj.has_key('group_id') else 0

    #支持members参数为对象数组
    #[{uid:"", name:"", avatar:"可选"}, ...]
    memberIDs = map(lambda m:m['uid'] if type(m) == dict else m, members)
    
    if gid > 0:
        gid = Group.create_group_ext(g._db, gid, appid, master, name, 
                                     is_super, memberIDs)
    else:
        gid = Group.create_group(g._db, appid, master, name, 
                                 is_super, memberIDs)
    
    s = 1 if is_super else 0
    content = "%d,%d,%d"%(gid, appid, s)
    publish_message(g.rds, "group_create", content)
    
    for mem in memberIDs:
        content = "%d,%d"%(gid, mem)
        publish_message(g.rds, "group_member_add", content)
    
    v = {
        "group_id":gid, 
        "master":master, 
        "name":name, 
        "members":members,
        "timestamp":int(time.time())
    }
    op = {"create":v}
    send_group_notification(appid, gid, op, members)

    resp = {"data":{"group_id":gid}}
    return make_response(200, resp)



@app.route("/groups/<int:gid>", methods=["DELETE"])
@require_application_auth
def delete_group(gid):
    appid = request.appid

    group = Group.get_group(g._db, gid)
    if not group:
        raise ResponseMeta(400, "group non exists")
    
    Group.disband_group(g._db, gid)
    v = {
        "group_id":gid,
        "group_name":group['name'],
        "timestamp":int(time.time())
    }
    op = {"disband":v}
    send_group_notification(appid, gid, op, None)

    content = "%d"%gid
    publish_message(g.rds, "group_disband", content)

    resp = {"success":True}
    return make_response(200, resp)



@app.route("/groups/<int:gid>/upgrade", methods=["POST"])
@require_application_auth
def upgrade_group(gid):
    """从普通群升级为超级群"""
    appid = request.appid
    group = Group.get_group(g._db, gid)

    members = Group.get_group_members(g._db, gid)

    if not group:
        raise ResponseMeta(400, "group non exists")

    Group.update_group_super(g._db, gid, 1)

    content = "%d,%d,%d"%(gid, appid, 1)
    publish_message(g.rds, "group_upgrade", content)

    v = {
        "group_id":gid,
        "group_name":group['name'],
        "timestamp":int(time.time()),
        "super":1
    }
    op = {"upgrade":v}
    send_group_notification(appid, gid, op, None)

    resp = {"success":True}
    return make_response(200, resp)


@app.route("/groups/<int:gid>", methods=["PATCH"])
@require_application_auth
def update_group(gid):
    appid = request.appid
    obj = json.loads(request.data)
    name = obj["name"]
    Group.update_group_name(g._db, gid, name)

    v = {
        "group_id":gid,
        "timestamp":int(time.time()),
        "name":name
    }
    op = {"update_name":v}
    send_group_notification(appid, gid, op, None)

    return ""


@app.route("/groups/<int:gid>/members", methods=["POST"])
@require_application_auth
def add_group_member(gid):
    appid = request.appid
    obj = json.loads(request.data)
    inviter = None
    if type(obj) is dict:
        if 'members' in obj:
            members = obj['members']
            inviter = obj.get('inviter')
        else:
            members = [obj]
    else:
        members = obj

    if len(members) == 0:
        return ""
    
    group = Group.get_group(g._db, gid)
    if not group:
        raise ResponseMeta(400, "group non exists")
    
    # 支持members参数为对象数组
    memberIDs = map(lambda m:m['uid'] if type(m) == dict else m, members)
    
    g._db.begin()
    for member_id in memberIDs:
        try:
            Group.add_group_member(g._db, gid, member_id)
            User.reset_group_synckey(g.rds, appid, member_id, gid)
        except pymysql.err.IntegrityError, e:
            # 可能是重新加入群
            # 1062 duplicate member
            if e[0] != 1062:
                raise

    g._db.commit()

    for m in members:
        member_id = m['uid'] if type(m) == dict else m
        v = {
            "group_id":gid,
            "group_name":group['name'],
            "member_id":member_id,
            "timestamp":int(time.time())
        }
        if type(m) == dict and m.get('name'):
            v['name'] = m['name']
        if type(m) == dict and m.get('avatar'):
            v['avatar'] = m['avatar']
        if inviter:
            v['inviter'] = inviter

        op = {"add_member":v}
        send_group_notification(appid, gid, op, [member_id])
         
        content = "%d,%d"%(gid, member_id)
        publish_message(g.rds, "group_member_add", content)

    resp = {"success":True}
    return make_response(200, resp)


def remove_group_member(appid, gid, group_name, member):
    memberid = member['uid']    
    Group.delete_group_member(g._db, gid, memberid)
         
    v = {
        "group_id":gid,
        "group_name":group_name,
        "member_id":memberid,
        "timestamp":int(time.time())
    }
    if member.get('name'):
        v['name'] = member['name']
    if member.get('avatar'):
        v['avatar'] = member['avatar']
    
    op = {"quit_group":v}
    send_group_notification(appid, gid, op, [memberid])
     
    content = "%d,%d"%(gid,memberid)
    publish_message(g.rds, "group_member_remove", content)
    
@app.route("/groups/<int:gid>/members/<int:memberid>", methods=["DELETE"])
@require_application_auth
def leave_group_member(gid, memberid):
    appid = request.appid
    group = Group.get_group(g._db, gid)
    if not group:
        raise ResponseMeta(400, "group non exists")

    name = User.get_user_name(g.rds, appid, memberid)
    m = {"uid":memberid}
    if name:
        m['name'] = name
    remove_group_member(appid, gid, group['name'], m)
    
    resp = {"success":True}
    return make_response(200, resp)


@app.route("/groups/<int:gid>/members", methods=["DELETE"])
@require_application_auth
def delete_group_member(gid):
    appid = request.appid
    members = json.loads(request.data)
    if len(members) == 0:
        raise ResponseMeta(400, "no memebers to delete")

    group = Group.get_group(g._db, gid)
    if not group:
        raise ResponseMeta(400, "group non exists")
    
    for m in members:
        if type(m) == int:
            member = {"uid":m}
        else:
            member = m
            
        remove_group_member(appid, gid, group['name'], member)

    resp = {"success":True}
    return make_response(200, resp)


@app.route("/groups/<int:gid>/members/<int:memberid>", methods=["PATCH"])
@require_application_auth
def group_member_setting(gid, memberid):
    appid = request.appid
    uid = memberid

    group = Group.get_group(g._db, gid)
    if not group:
        raise ResponseMeta(400, "group non exists")
    
    obj = json.loads(request.data)
    if obj.has_key('do_not_disturb'):
        User.set_group_do_not_disturb(g.rds, appid, uid, gid, obj['do_not_disturb'])
    elif obj.has_key('nickname'):
        Group.update_nickname(g._db, gid, uid, obj['nickname'])
        v = {
            "group_id":gid,
            "group_name":group['name'],
            "timestamp":int(time.time()),
            "nickname":obj['nickname'],
            "member_id":uid
        }
        op = {"update_member_nickname":v}
        send_group_notification(appid, gid, op, None)
    elif obj.has_key('mute'):
        mute = 1 if obj['mute'] else 0
        Group.update_mute(g._db, gid, uid, mute)
        content = "%d,%d,%d" % (gid, memberid, mute)
        publish_message(g.rds, "group_member_mute", content)
    else:
        raise ResponseMeta(400, "no action")

    resp = {"success":True}
    return make_response(200, resp)
