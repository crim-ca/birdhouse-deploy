#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
These hooks will be running within Twitcher, using MagpieAdapter context, applied for Weaver requests.

The code below can make use of any package that is installed by Magpie/Twitcher.

.. seealso::
    Documentation about Magpie/Twitcher request/response hooks is available here:
    https://pavics-magpie.readthedocs.io/en/latest/configuration.html#service-hooks
"""

import json
from typing import TYPE_CHECKING

import transaction

from magpie.api.management.resource import resource_utils as ru
from magpie.api.management.user import user_utils as uu
from magpie.api.requests import get_user, get_service_matchdict_checked
from magpie.constants import get_constant
from magpie.models import Route, Service
from magpie.register import magpie_register_permissions_from_config
from magpie.permissions import Access, Permission, PermissionSet, Scope
from magpie.utils import get_header, get_logger

if TYPE_CHECKING:
    from pyramid.request import Request
    from pyramid.response import Response

    from magpie.adapter import HookContext

LOGGER = get_logger("birdhouse-weaver-hooks")


def is_admin(request):
    # type: (Request) -> bool
    admin_group = get_constant("MAGPIE_ADMIN_GROUP", settings_container=request)
    if not request.user:  # no user authenticated (public)
        return False
    return admin_group in [group.group_name for group in request.user.groups]


def add_x_wps_output_context(request):
    # type: (Request) -> Request
    """
    Apply the ``X-WPS-Output-Context`` for saving outputs in the user-context WPS-outputs directory.
    """
    header = get_header("X-WPS-Output-Context", request.headers)
    # if explicitly provided, ensure it is permitted (admin allow any, otherwise self-user reference only)
    if header is not None:
        if request.user is None:
            header = "public"
        else:
            if not is_admin(request):
                # override disallowed writing to other location
                # otherwise, up to admin to have written something sensible
                header = "users/" + str(request.user.id)
    else:
        if request.user is None:
            header = "public"
        else:
            header = "users/" + str(request.user.id)
    request.headers["X-WPS-Output-Context"] = header
    return request


def filter_allowed_processes(response, context):
    # type: (Response, HookContext) -> Response
    """
    Filter processes returned by Weaver response according to allowed resources by user.

    Following are sample (clipped) JSON body that can be expected from Weaver (or any OGC API - Processes).

    Using ``GET https://<weaver.url>/processes``

    .. code-block:: json
        :caption: Detailed process listing from Weaver (other fields than 'processes' are removed for concise example).

        {
          "processes": [
            {
              "id": "ColibriFlyingpigeon_SubsetBbox",
              "title": "ColibriFlyingpigeon_SubsetBbox",
              "mutable": true,
              "keywords": [
                "application"
              ],
              "metadata": [],
              "jobControlOptions": [
                "async-execute"
              ],
              "outputTransmission": [
                "reference",
                "value"
              ],
              "processDescriptionURL": "https://<weaver.url>/processes/ColibriFlyingpigeon_SubsetBbox",
              "processEndpointWPS1": "https://<weaver.url>/ows/wps",
              "executeEndpoint": "https://<weaver.url>/processes/ColibriFlyingpigeon_SubsetBbox/jobs"
            }
          ]
        }

    Using ``GET https://<weaver.url>/processes?detail=false``

    .. code-block:: json
        :caption: Simple process listing from Weaver (other fields than 'processes' are removed for concise example).

        {
          "description": "Listing of available processes successful.",
          "processes": [
            "CatFile",
            "ColibriFlyingpigeon_SubsetBbox",
          ],
          "page": 0,
          "total": 2
        }

    """
    if "application/json" in response.content_type:
        body = response.json
        if "processes" in body:
            if is_admin(response.request):  # don't waste time checking permissions, full access anyway
                return response

            # depending on 'detail' query, processes can be returned as list of IDs or nested JSON summaries
            processes = {
                proc if isinstance(proc, str) else proc.get("id"): proc
                for proc in body["processes"]
            }

            # only need 2 first levels ('processes' and each process 'id' under it)
            children = ru.get_resource_children(context.resource, response.request.db, limit_depth=2)
            proc_res = None
            for res in children.values():
                if res["node"].resource_name == "processes":
                    # if nothing under 'processes' resource, then guarantee no permissions, done check
                    if not res["children"]:
                        return response
                    proc_res = res
                    break
            if not proc_res:
                return response  # 'processes' itself does not exist, no permissions possible and done check

            allowed_processes = []
            known_processes = proc_res["children"].values()
            known_processes = {res["node"].resource_name: res for res in known_processes}
            request_user = get_user(response.request)
            for proc_name in processes:
                if proc_name not in known_processes:
                    continue  # do not bother checking missing resource
                child_proc = known_processes[proc_name]["node"]
                perms = context.service.effective_permissions(request_user, child_proc, [Permission.READ])
                if perms[0].access == Access.ALLOW:
                    proc = processes[proc_name]
                    allowed_processes.append(proc)

            # override collected and permitted processes access by user
            body["processes"] = allowed_processes

            # WARNING:
            #  JSON generated from 'body' attribute cannot be overridden directly (computed inline).
            #  Also, since we override, must set any Content header accordingly with modifications.
            data = json.dumps(body).encode("UTF-8")
            response.body = data
            c_len = len(data)
            response.content_length = c_len
            response.headers["Content-Length"] = str(c_len)

    return response


def allow_user_execute_outputs(response):
    # type: (Response) -> Response
    """
    Allow the authenticated user executing the process to access the expected output location.

    This ensures that, when ``optional-components/secure-data-proxy`` is enabled, the user will be able to retrieve
    the result files stored under the ``/wpsoutputs/users/<user-id>`` directory, by applying the corresponding
    permission if missing.
    """
    request = response.request
    user = request.user
    if user is None or is_admin(request):
        return response

    session = request.db
    service = Service.by_service_name("secure-data-proxy", db_session=session)
    if not service:  # optional component not enabled, nothing to be set (public access expected)
        return response

    with transaction.manager:
        config = {
            "permissions": [
                {
                    "service": service.resource_name,
                    "resource": f"/wpsoutputs/users/{user.id}",
                    "type": "route",
                    "user": user.user_name,
                    "action": "create",
                 }
            ]
        }
        magpie_register_permissions_from_config(config, db_session=session)
        transaction.commit()

    return response


def allow_user_deployed_processes(response):
    # type: (Response) -> Response
    """
    Add the user permissions to read (listing and description) and execute the process for the deploying user.

    This will grant access to the process definition by the user that deployed it until they desire to make it public.
    At a later time, a request to the appropriate group to share (restricted group) or to publish publicly (anonymous)
    could be made to create the relevant permissions to describe or execute the process by other users.

    Expected response format from service:

    .. code-block:: json
        {
          "processSummary": {
            "id": "<deployed-process-id>",
            "..."
          }
        }

    If any failure occurs, simply return the response to let deployment succeed, but user will not receive access to it
    automatically. Manual update of permissions would be necessary by platform administrator via Magpie.
    """
    p_id = "<undefined>"
    u_name = "<undefined>"
    try:
        # only apply permission if deployment was successful
        if "application/json" in response.content_type and response.status_code == 201:
            body = response.json
            info = body.get("processSummary", {}) or body.get("process", {})  # bw-compat
            p_id = info.get("id")
            if not (p_id and isinstance(p_id, str)):
                return response

            # user is not necessarily admin
            # in fact, this operation is only needed if non-admin, since admin has full access anyway
            request = response.request
            if is_admin(request):
                return response
            user = request.user
            # if deploy endpoint was made public, then even anonymous could deploy (not recommended, but possible)
            if not user:
                user = get_user(request)
            u_name = user.user_name

            # note: matchdict reference of Twitcher owsproxy view is used, just so happens to be same name as Magpie
            service = get_service_matchdict_checked(request)

            # find the nested resource matching: "weaver/processes/<p_id>"
            children = ru.get_resource_children(service, request.db, limit_depth=2)
            p_res_create = False
            p_res = None
            for res in children.values():
                if res["node"].resource_name == "processes":
                    processes_res_id = res["node"].resource_id
                    for child_res in res["children"].values():
                        if child_res["node"].resource_name == p_id:
                            p_res = child_res["node"]
                            break
                    break
            else:
                p_res_create = True  # must create after in new transaction context

            # note:
            #  since this is running within a *response* hook, the request transaction is already handled
            #  define a new transaction to create new resources
            with transaction.manager:
                session = request.db
                if p_res_create:
                    # resource 'processes' should already exist, but create it if somehow missing
                    # otherwise, it will be impossible to create '<p_id>' under it
                    resp = ru.create_resource("processes", None, Route.resource_type_name, service.resource_id, session)
                    processes_res_id = resp.json["resource"]["resource_id"]

                # if '<p_id>' somehow already exists, use it
                if p_res is None:
                    resp = ru.create_resource(p_id, None, Route.resource_type_name, processes_res_id, session)
                    p_res_id = resp.json["resource"]["resource_id"]
                    p_res = ru.ResourceService.by_resource_id(p_res_id, session)
                if not p_res:
                    LOGGER.warning(
                        "Failed creation of permissions for user [%s] to access deployed process [%s] in Weaver. "
                        "Could not retrieve resource matching deployed process!", u_name, p_id
                    )
                    return response

                # apply necessary permissions to give full access to the deployed process to the user
                # override permissions to undo what could have been previously applied (only if <p_id> already existed)
                p_desc = PermissionSet(Permission.READ, Access.ALLOW, Scope.RECURSIVE)   # describe proc + jobs statuses
                p_exec = PermissionSet(Permission.WRITE, Access.ALLOW, Scope.RECURSIVE)  # edit process + execute jobs
                r_desc = uu.create_user_resource_permission_response(user, p_res, p_desc, session, overwrite=True)
                r_exec = uu.create_user_resource_permission_response(user, p_res, p_exec, session, overwrite=True)

                # summit transaction results (new resources and permissions)
                transaction.commit()

            # sanity check
            if r_desc.status_code in [200, 201] and r_exec.status_code in [200, 201]:
                LOGGER.info(
                    "Successful creation of permissions for user [%s] to access deployed process [%s] in Weaver.",
                    u_name, p_id
                )
            else:
                statuses = [r_desc.status_code, r_exec.status_code]
                LOGGER.warning(
                    "Failed creation of permissions for user [%s] to access deployed process [%s] in Weaver. "
                    "Permission creation returned unexpected statuses: %s", u_name, p_id, statuses
                )
    except Exception as exc:
        LOGGER.error(
            "Failed creation of permissions for user [%s] to access deployed process [%s] in Weaver. "
            "Unexpected exception occurred: [%s]", u_name, p_id, str(exc)
        )

    return response
