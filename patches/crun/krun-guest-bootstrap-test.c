/* ABOUTME: Behavioral checks for the isolated libkrun guest-config rewrite. */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "krun-guest-bootstrap.h"

static void
assert_root_user (const char *input)
{
  char *output = NULL;
  json_object *config;
  json_object *process;
  json_object *user;
  json_object *uid;
  json_object *gid;

  assert (nas_krun_guest_bootstrap_root_config (input, strlen (input), &output) == 0);
  assert (output != NULL);
  assert (strcmp (input, output) != 0);
  config = json_tokener_parse (output);
  assert (config != NULL);
  assert (json_object_object_get_ex (config, "process", &process));
  assert (json_object_object_get_ex (process, "user", &user));
  assert (json_object_object_get_ex (user, "uid", &uid));
  assert (json_object_get_int (uid) == 0);
  assert (json_object_object_get_ex (user, "gid", &gid));
  assert (json_object_get_int (gid) == 0);
  {
    json_object *args;
    json_object *linux_config;
    json_object *mappings;
    json_object *mapping;
    json_object *host_id;
    assert (json_object_object_get_ex (process, "args", &args));
    assert (json_object_array_length (args) == 1);
    assert (strcmp (json_object_get_string (json_object_array_get_idx (args, 0)), "/entrypoint") == 0);
    assert (json_object_object_get_ex (config, "linux", &linux_config));
    assert (json_object_object_get_ex (linux_config, "uidMappings", &mappings));
    assert (json_object_array_length (mappings) == 1);
    mapping = json_object_array_get_idx (mappings, 0);
    assert (json_object_object_get_ex (mapping, "hostID", &host_id));
    assert (json_object_get_int64 (host_id) == 512150000);
  }
  json_object_put (config);
  free (output);
}

int
main (void)
{
  const char *normal = "{\"process\":{\"user\":{\"uid\":1000,\"gid\":1000},\"args\":[\"/entrypoint\"]},\"linux\":{\"uidMappings\":[{\"containerID\":0,\"hostID\":512150000,\"size\":65536}]}}";
  char *original = strdup (normal);
  char *output = NULL;

  assert (original != NULL);
  assert_root_user (normal);
  assert (strcmp (normal, original) == 0);
  const char *missing_user = "{\"process\":{\"args\":[\"/entrypoint\"]}}";
  const char *invalid_uid = "{\"process\":{\"user\":{\"uid\":\"1000\",\"gid\":1000}}}";
  assert (nas_krun_guest_bootstrap_root_config (
              missing_user, strlen (missing_user), &output)
          != 0);
  assert (output == NULL);
  assert (nas_krun_guest_bootstrap_root_config ("not json", strlen ("not json"), &output) != 0);
  assert (output == NULL);
  assert (nas_krun_guest_bootstrap_root_config (
              invalid_uid, strlen (invalid_uid), &output)
          != 0);
  assert (output == NULL);
  assert (strcmp (normal, original) == 0);
  free (original);
  puts ("guest bootstrap config rewrite: PASS");
  return 0;
}
