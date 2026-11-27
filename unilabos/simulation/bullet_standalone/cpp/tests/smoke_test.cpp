/* 独立 Bullet 库的 C++ 最小闭环：盒体落地、射线、铰链。
 * 只使用 rb_abi.h 的 C ABI，构建时通过 -DUNILAB_BULLET_BUILD_SMOKE_TEST=ON 启用。 */

#include "../src/rb_abi.h"

#include <cmath>
#include <cstdio>

namespace {

int failures = 0;

void check(bool condition, const char *message)
{
  if (condition) {
    std::printf("ok   %s\n", message);
  }
  else {
    std::printf("FAIL %s\n", message);
    ++failures;
  }
}

rbDynamicsWorld *make_world(float gz = -9.81f)
{
  const float gravity[3] = {0.0f, 0.0f, gz};
  rbDynamicsWorld *world = RB_dworld_new(gravity);
  RB_dworld_set_solver_iterations(world, 1000);
  RB_dworld_set_split_impulse(world, 1);
  return world;
}

rbRigidBody *add_box(rbDynamicsWorld *world,
                     const float half[3],
                     const float position[3],
                     float mass,
                     int mask,
                     rbCollisionShape **shape_out = nullptr)
{
  rbCollisionShape *shape = RB_shape_new_box(half[0], half[1], half[2]);
  RB_shape_set_margin(shape, 0.0001f);
  const float identity[4] = {1.0f, 0.0f, 0.0f, 0.0f};
  rbRigidBody *body = RB_body_new(shape, position, identity);
  RB_body_set_mass(body, mass);
  RB_body_set_friction(body, 0.8f);
  RB_body_set_damping(body, 0.0f, 0.0f);
  RB_body_set_sleep_thresh(body, 0.0f, 0.0f);
  RB_body_set_activation_state(body, 0);
  RB_dworld_add_body(world, body, mask);
  if (shape_out) {
    *shape_out = shape;
  }
  return body;
}

void step(rbDynamicsWorld *world, int count)
{
  for (int i = 0; i < count; ++i) {
    RB_dworld_step_simulation(world, 1.0f / 240.0f, 0, 1.0f / 240.0f);
  }
}

}  // namespace

int main()
{
  check(lab_scalar_bytes() == 8, "Bullet 标量为 double");
  check(lab_abi_version() == 1, "ABI 版本为 1");

  rbDynamicsWorld *world = make_world();
  const float ground_half[3] = {0.5f, 0.5f, 0.05f};
  const float ground_pos[3] = {0.0f, 0.0f, -0.05f};
  rbRigidBody *ground = add_box(world, ground_half, ground_pos, 0.0f, 1);

  const float box_half[3] = {0.02f, 0.02f, 0.02f};
  const float box_pos[3] = {0.0f, 0.0f, 0.30f};
  rbRigidBody *box = add_box(world, box_half, box_pos, 0.1f, 1);
  step(world, 480);

  float position[3];
  RB_body_get_position(box, position);
  check(std::fabs(position[2] - 0.02f) < 2e-3f, "盒体落到地面高度");

  rbContact contacts[64];
  const int contact_count = lab_contacts(world, contacts, 64);
  check(contact_count > 0, "落地后存在接触流");
  bool ground_box_pair = false;
  double total_impulse = 0.0; double peak_impulse = 0.0;
  for (int i = 0; i < contact_count; ++i) {
    if ((contacts[i].a == ground && contacts[i].b == box) ||
        (contacts[i].a == box && contacts[i].b == ground)) {
      ground_box_pair = true;
      peak_impulse = std::fmax(peak_impulse, contacts[i].impulse); total_impulse += contacts[i].impulse;
    }
  }
  check(ground_box_pair && total_impulse * 240.0 > 0.5, "地面/盒体法向冲量");

  double distance = 0.0;
  const double ray_from[3] = {0.2, 0.0, 0.08};
  const double ray_to[3] = {0.2, 0.0, -0.02};
  void *hit = lab_ray(world, ray_from, ray_to, nullptr, &distance);
  check(hit == ground && distance > 0.075 && distance < 0.081, "射线命中地面距离");
  hit = lab_ray(world, ray_from, ray_to, ground, &distance);
  check(hit == nullptr, "射线排除指定刚体");

  const float anchor_half[3] = {0.01f, 0.01f, 0.01f};
  const float anchor_pos[3] = {0.0f, 0.0f, 0.0f};
  const float rod_half[3] = {0.05f, 0.005f, 0.005f};
  const float rod_pos[3] = {0.05f, 0.0f, 0.0f};
  rbDynamicsWorld *hinge_world = make_world(0.0f);
  rbRigidBody *anchor = add_box(hinge_world, anchor_half, anchor_pos, 0.0f, 1);
  rbRigidBody *rod = add_box(hinge_world, rod_half, rod_pos, 0.05f, 1);
  lab_ignore(anchor, rod, 1);

  float hinge_pivot[3] = {0.0f, 0.0f, 0.0f};
  float hinge_orn[4] = {1.0f, 0.0f, 0.0f, 0.0f};
  rbConstraint *hinge = RB_constraint_new_hinge(hinge_pivot, hinge_orn, anchor, rod);
  RB_dworld_add_constraint(hinge_world, hinge, 1);
  RB_constraint_set_solver_iterations(hinge, 1000);
  RB_constraint_set_limits_hinge(hinge, 1.0f, -1.0f);
  const float spin[3] = {0.0f, 0.0f, -2.0f};
  RB_body_set_angular_velocity(rod, spin);
  step(hinge_world, 60);

  RB_body_get_position(rod, position);
  const double radius = std::sqrt(position[0] * position[0] + position[1] * position[1]);
  const double angle = std::atan2(position[1], position[0]);
  check(std::fabs(radius - 0.05) < 2e-3, "铰链保持转轴半径");
  check(angle < -0.05 && angle > -0.20, "铰链绕 pivot 旋转约 -0.126 rad");

  RB_constraint_delete(hinge);
  RB_dworld_remove_body(hinge_world, rod);
  RB_body_delete(rod);
  RB_dworld_remove_body(hinge_world, anchor);
  RB_body_delete(anchor);
  RB_dworld_remove_body(world, box);
  RB_body_delete(box);
  RB_dworld_remove_body(world, ground);
  RB_body_delete(ground);
  RB_dworld_delete(hinge_world);
  RB_dworld_delete(world);

  std::printf(failures == 0 ? "SMOKE OK\n" : "SMOKE FAILED (%d)\n", failures);
  return failures == 0 ? 0 : 1;
}
