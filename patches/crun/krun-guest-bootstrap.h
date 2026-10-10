/* Helpers for the opt-in guest bootstrap identity annotation. */
#ifndef NAS_KRUN_GUEST_BOOTSTRAP_H
#define NAS_KRUN_GUEST_BOOTSTRAP_H

#include <json-c/json.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>

/* Return a rewritten copy of an OCI config, without changing the input. */
static int
nas_krun_guest_bootstrap_root_config (const char *input, size_t input_len, char **output)
{
  struct json_tokener *tokener = NULL;
  json_object *config = NULL;
  json_object *process = NULL;
  json_object *user = NULL;
  json_object *uid = NULL;
  json_object *gid = NULL;
  enum json_tokener_error parse_error;
  const char *serialized;
  size_t parsed_len;
  size_t i;
  int ret = -1;

  *output = NULL;
  if (input_len > INT_MAX)
    return -1;

  tokener = json_tokener_new ();
  if (tokener == NULL)
    return -1;
  config = json_tokener_parse_ex (tokener, input, (int) input_len);
  parse_error = json_tokener_get_error (tokener);
  parsed_len = json_tokener_get_parse_end (tokener);
  if (parse_error != json_tokener_success || config == NULL)
    goto cleanup;
  for (i = parsed_len; i < input_len; i++)
    if (input[i] != ' ' && input[i] != '\t' && input[i] != '\n' && input[i] != '\r')
      goto cleanup;
  if (! json_object_is_type (config, json_type_object)
      || ! json_object_object_get_ex (config, "process", &process)
      || ! json_object_is_type (process, json_type_object)
      || ! json_object_object_get_ex (process, "user", &user)
      || ! json_object_is_type (user, json_type_object)
      || ! json_object_object_get_ex (user, "uid", &uid)
      || ! json_object_is_type (uid, json_type_int)
      || ! json_object_object_get_ex (user, "gid", &gid)
      || ! json_object_is_type (gid, json_type_int))
    goto cleanup;

  {
    json_object *root_id = json_object_new_int (0);
    json_object *root_gid = json_object_new_int (0);
    if (root_id == NULL || root_gid == NULL)
      {
        if (root_id != NULL)
          json_object_put (root_id);
        if (root_gid != NULL)
          json_object_put (root_gid);
        goto cleanup;
      }
    if (json_object_object_add (user, "uid", root_id) != 0)
      {
        json_object_put (root_id);
        json_object_put (root_gid);
        goto cleanup;
      }
    if (json_object_object_add (user, "gid", root_gid) != 0)
      {
        json_object_put (root_gid);
        goto cleanup;
      }
  }
  serialized = json_object_to_json_string_ext (config, JSON_C_TO_STRING_PLAIN);
  if (serialized == NULL)
    goto cleanup;
  *output = strdup (serialized);
  if (*output == NULL)
    goto cleanup;
  ret = 0;

cleanup:
  if (config != NULL)
    json_object_put (config);
  json_tokener_free (tokener);
  return ret;
}

#endif /* NAS_KRUN_GUEST_BOOTSTRAP_H */
